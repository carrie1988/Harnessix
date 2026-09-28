"""验证公开驼峰预算到领域预算的无损转换及原命令幂等边界。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.models import Budget
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService, _budget
from harnessix.models.contracts import ResponseFailed
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.protocol.contracts import PublicBudget
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.helpers import wait_for_turn_status


def _limits() -> PublicBudget:
    return PublicBudget(
        max_steps=8,
        max_tokens=32000,
        timeout_seconds=120.0,
        max_output_chars=32000,
        max_tool_calls_per_step=1,
    )


@pytest.mark.parametrize(
    "values",
    (
        (1, 1, 0.5, 1, 1),
        (8, 32000, 120.0, 32000, 1),
        (1000, 1_000_000, 86400.0, 1_000_000, 128),
    ),
)
def test_public_budget_maps_all_fields_without_changing_wire_aliases(values: tuple) -> None:
    public = PublicBudget(**dict(zip(PublicBudget.model_fields, values, strict=True)))
    expected = public.model_dump(by_alias=False)
    assert set(public.model_dump()) == {
        "maxSteps",
        "maxTokens",
        "timeoutSeconds",
        "maxOutputChars",
        "maxToolCallsPerStep",
    }
    assert _budget(public) == Budget.model_validate(expected)
    assert _budget(public).model_dump() == expected


def test_absent_public_budget_preserves_domain_default_selection() -> None:
    assert _budget(None) is None


async def test_sdk_explicit_budget_is_durable_and_duplicate_keeps_original_command(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = FakeProvider("预算契约已验证")
    limits = _limits()
    client_id = uuid4()
    async with AgentRuntime(store, provider) as runtime:
        requests = SQLiteProtocolRequestStore(store.path)
        service = AgentApplicationService(runtime, store, requests)
        client = AgentClient(
            InProcessAgentTransport(AgentProtocolServer(service)),
            client_instance_id=client_id,
        )
        try:
            await client.initialize()
            thread = await client.create_thread(str(tmp_path), request_id="thread")
            accepted = await client.start_turn(
                thread.thread_id, "检查预算", request_id="turn", budget=limits
            )
            assert accepted.budget == limits
            current = await wait_for_turn_status(client, thread.thread_id, "completed")
            assert current.latest_turn is not None and current.latest_turn.budget == limits
            duplicate = await client.start_turn(
                thread.thread_id, "检查预算", request_id="turn", budget=limits
            )
            assert duplicate.turn_id == accepted.turn_id
            with pytest.raises(AgentSDKError) as conflict:
                await client.start_turn(
                    thread.thread_id,
                    "检查预算",
                    request_id="turn",
                    budget=limits.model_copy(update={"max_tokens": 32001}),
                )
            assert conflict.value.code == "idempotency_conflict"
            record = await requests.get(client_id, "turn")
            assert record is not None and record.state == "completed"
            assert len(provider.requests) == 1
        finally:
            await client.close()

    # 用原Store重开，确认不是只修好了响应投影，实际持久预算也完全相同。
    async with AgentRuntime(store, FakeProvider()) as runtime:
        persisted = await store.get_thread(thread.thread_id)
        assert len(persisted.turns) == 1
        assert persisted.turns[0].budget.model_dump() == limits.model_dump(by_alias=False)


async def test_sdk_explicit_retry_budget_keeps_source_terminal_and_retry_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    failed_provider = ScriptedProvider([[ResponseFailed(code="authentication")]])
    async with AgentRuntime(store, failed_provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        source = await runtime.run_turn(thread.thread_id, "固定失败", request_id="source")
    assert source.status.value == "failed"

    provider = FakeProvider("显式重试已完成")
    limits = _limits()
    async with AgentRuntime(store, provider) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        try:
            await client.initialize()
            retry = await client.retry_turn(
                thread.thread_id, source.turn_id, request_id="retry", budget=limits
            )
            assert retry.budget == limits
            await wait_for_turn_status(client, thread.thread_id, "completed")
            duplicate = await client.retry_turn(
                thread.thread_id, source.turn_id, request_id="retry", budget=limits
            )
            assert duplicate.turn_id == retry.turn_id
            persisted = await store.get_thread(thread.thread_id)
            assert len(persisted.turns) == 2
            assert persisted.turns[0] == source
            assert persisted.turns[1].retry_of_turn_id == source.turn_id
            assert persisted.turns[1].budget.model_dump() == limits.model_dump(by_alias=False)
            assert len(provider.requests) == 1
        finally:
            await client.close()


@pytest.mark.parametrize(
    "field,value",
    (
        ("maxSteps", 0),
        ("maxTokens", True),
        ("timeoutSeconds", 0),
        ("maxOutputChars", 1_000_001),
        ("maxToolCallsPerStep", 129),
    ),
)
async def test_invalid_wire_budget_is_rejected_before_claim_or_turn(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = FakeProvider()
    async with AgentRuntime(store, provider) as runtime:
        requests = SQLiteProtocolRequestStore(store.path)
        service = AgentApplicationService(runtime, store, requests)
        server = AgentProtocolServer(service)
        client = AgentClient(InProcessAgentTransport(server))
        try:
            await client.initialize()
            thread = await client.create_thread(str(tmp_path), request_id="thread")
            wire = _limits().model_dump(mode="json")
            wire[field] = value
            frame = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 99,
                        "method": "turn/start",
                        "params": {
                            "requestId": "invalid-budget",
                            "threadId": str(thread.thread_id),
                            "prompt": "不得执行",
                            "budget": wire,
                        },
                    }
                ).encode()
                + b"\n"
            )
            (response,) = await server.process_frame(frame)
            error = json.loads(response)["error"]["data"]
            assert error["code"] == "invalid_params"
            assert error["path"] == ["budget", field]
            assert server.client_instance_id is not None
            assert await requests.get(server.client_instance_id, "invalid-budget") is None
            assert not (await store.get_thread(thread.thread_id)).turns
            assert provider.requests == []
        finally:
            await client.close()
