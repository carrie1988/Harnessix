"""实际SDK、SQLite命令回执及保护顺序，不调用网络Provider。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.command_runtime import AgentServiceError, execute_command
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import ThreadCreateParams, ThreadResult, TurnCancelParams
from harnessix.protocol.projection import project_thread
from harnessix.protocol.requests import ProtocolRequestError, SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY, protected


@pytest.mark.parametrize("field", ["workspace", "request_id", "method"])
async def test_command_preflight_never_touches_request_store(tmp_path, field):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            requests = AsyncMock()
            operation = AsyncMock()
            with pytest.raises(AgentServiceError) as caught:
                await execute_command(
                    runtime,
                    requests,
                    uuid4(),
                    CANARY if field == "method" else "thread/create",
                    ThreadCreateParams(
                        workspace=str(tmp_path / CANARY) if field == "workspace" else str(tmp_path),
                        request_id=CANARY if field == "request_id" else "safe",
                    ),
                    operation,
                    ThreadResult,
                )
            assert caught.value.code == "public_input_secret_leak"
            assert CANARY not in str(caught.value) and not operation.await_count
            assert not requests.mock_calls


@pytest.mark.parametrize("field", ["prompt", "request_id"])
async def test_sdk_rejection_has_no_turn_receipt_replay_or_database_value(tmp_path, field):
    path = tmp_path / "s.db"
    provider = ScriptedProvider([answer()])
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(path), provider, public_output_protection=scope
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            requests = SQLiteProtocolRequestStore(path)
            service = AgentApplicationService(runtime, runtime.store, requests)
            client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
            await client.initialize()
            try:
                with pytest.raises(AgentSDKError) as caught:
                    await client.start_turn(
                        thread.thread_id,
                        CANARY if field == "prompt" else "safe",
                        request_id=CANARY if field == "request_id" else "safe",
                    )
                assert caught.value.code == "public_input_secret_leak"
                assert not provider.requests
                assert await runtime.store.get_thread(thread.thread_id) == thread
                assert (
                    await requests.get(
                        client.client_instance_id, CANARY if field == "request_id" else "safe"
                    )
                    is None
                )
                assert (
                    CANARY not in (await client.replay_events(thread.thread_id)).model_dump_json()
                )
            finally:
                await client.close()
    assert not any(CANARY.encode() in p.read_bytes() for p in tmp_path.glob("s.db*"))


@pytest.mark.parametrize("state", ["completed", "failed"])
async def test_cached_registered_outcome_is_not_replayed_or_rewritten(tmp_path, state):
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([])) as runtime:
        legacy = await runtime.create_thread(str(tmp_path / CANARY))
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()
    params = ThreadCreateParams(workspace=str(tmp_path), request_id="cached")
    await requests.claim(
        client, params.request_id, "thread/create", params.model_dump(mode="json", by_alias=True)
    )
    if state == "completed":
        original = ThreadResult(thread=project_thread(legacy)).model_dump(
            mode="json", by_alias=True
        )
        await requests.complete(client, params.request_id, original)
    else:
        await requests.fail(
            client, params.request_id, {"code": "fixture", "message": CANARY, "retryable": False}
        )
    before = await requests.get(client, params.request_id)
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(store.path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            operation = AsyncMock()
            after_result = []
            with pytest.raises(AgentServiceError) as caught:
                await execute_command(
                    runtime,
                    requests,
                    client,
                    "thread/create",
                    params,
                    operation,
                    ThreadResult,
                    after_result=after_result.append,
                )
            assert caught.value.code == "public_output_secret_leak"
            assert not operation.await_count and not after_result
            assert await requests.get(client, params.request_id) == before


@pytest.mark.parametrize("mode", ["result", "kernel_error", "store_error"])
async def test_original_result_or_error_guard_precedes_receipt_publication(tmp_path, mode):
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([])) as runtime:
        legacy = await runtime.create_thread(str(tmp_path / CANARY))
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()
    params = ThreadCreateParams(workspace=str(tmp_path), request_id="r")

    async def operation():
        if mode == "kernel_error":
            raise KernelError("fixture", CANARY)
        if mode == "store_error":
            raise ProtocolRequestError("fixture", CANARY)
        return ThreadResult(thread=project_thread(legacy))

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(store.path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            after_result = []
            with pytest.raises(AgentServiceError) as caught:
                await execute_command(
                    runtime,
                    requests,
                    client,
                    "thread/create",
                    params,
                    operation,
                    ThreadResult,
                    after_result=after_result.append,
                )
            assert caught.value.code == "public_output_secret_leak"
            assert CANARY not in str(caught.value) and not after_result
            record = await requests.get(client, params.request_id)
            assert record.state == "failed" and CANARY not in record.model_dump_json()


async def test_lost_result_restart_replays_exact_safe_receipt_without_reexecuting(tmp_path):
    path = tmp_path / "s.db"
    client = uuid4()
    params = ThreadCreateParams(workspace=str(tmp_path), request_id="same")
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            service = AgentApplicationService(
                runtime, runtime.store, SQLiteProtocolRequestStore(path)
            )
            original = await service.create_thread(client, params)
            sequence = (await runtime.store.get_thread(original.thread.thread_id)).sequence
            await service.close()
        async with AgentRuntime(
            SQLiteSessionStore(path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            requests = SQLiteProtocolRequestStore(path)
            operation = AsyncMock()
            result = await execute_command(
                runtime, requests, client, "thread/create", params, operation, ThreadResult
            )
            assert result == original and not operation.await_count
            assert (await runtime.store.get_thread(original.thread.thread_id)).sequence == sequence
            with pytest.raises(AgentServiceError) as caught:
                await execute_command(
                    runtime,
                    requests,
                    client,
                    "thread/create",
                    ThreadCreateParams(workspace=str(tmp_path / "other"), request_id="same"),
                    operation,
                    ThreadResult,
                )
            assert caught.value.code == "idempotency_conflict"
            assert (await requests.get(client, "same")).state == "completed"


async def test_parent_cancel_before_claim_never_creates_receipt(tmp_path):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            requests = AsyncMock()

            async def cancelled():
                asyncio.current_task().cancel()
                await execute_command(
                    runtime,
                    requests,
                    uuid4(),
                    "thread/create",
                    ThreadCreateParams(workspace=str(tmp_path), request_id="r"),
                    AsyncMock(),
                    ThreadResult,
                )

            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(cancelled())
            assert not requests.mock_calls


async def test_closed_scope_blocks_sdk_command_but_direct_cancel_still_settles(tmp_path):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.accept_turn(thread.thread_id, "safe", request_id="r")
            requests = SQLiteProtocolRequestStore(runtime.store.path)
            service = AgentApplicationService(runtime, runtime.store, requests)
            scope.close()
            with pytest.raises(AgentServiceError) as caught:
                await service.cancel_turn(
                    uuid4(),
                    TurnCancelParams(
                        request_id="cancel", thread_id=thread.thread_id, turn_id=turn.turn_id
                    ),
                )
            assert caught.value.code == "public_input_secret_unavailable"
            assert (
                await runtime.cancel(thread.thread_id, turn.turn_id)
            ).status.value == "cancelled"
            await service.close()


@pytest.mark.parametrize("surface", ["rpc_id", "extra_param_key"])
async def test_raw_protocol_metadata_rejection_does_not_echo_registered_values(tmp_path, surface):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            service = AgentApplicationService(
                runtime, runtime.store, SQLiteProtocolRequestStore(runtime.store.path)
            )
            server = AgentProtocolServer(service)
            params = {
                "protocolVersion": "2.0",
                "clientInfo": {"name": "test", "version": "1"},
                "clientInstanceId": str(uuid4()),
            }
            if surface == "extra_param_key":
                params[CANARY] = "safe"
            frame = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": CANARY if surface == "rpc_id" else 1,
                        "method": "initialize",
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
            response = await server.process_frame(frame)
            # 固定旧版证据保留，现行实现必须在握手变更前拒绝原封套。
            assert CANARY.encode() not in response[0]
            assert json.loads(response[0])["error"]["data"]["code"] == "public_input_secret_leak"
            assert server.state.value == "new"
            await server.close()
