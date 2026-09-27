"""回调故障进入真实Runtime、Model历史、Session、Audit、Protocol与遥测的回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.trusted_actions.test_gateway_error_boundaries import fault_gateway
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload, _store_bytes


@pytest.mark.parametrize(
    "stage,read_only",
    [
        pytest.param("context", True, id="read-context"),
        pytest.param("output", True, id="read-output"),
        pytest.param("context", False, id="write-context"),
        pytest.param("review", False, id="write-review"),
        pytest.param("output", False, id="write-output"),
    ],
)
@pytest.mark.parametrize("error_kind", ["unknown", "registered", "runtime"])
async def test_gateway_errors_never_publish_internal_diagnostics_across_runtime(
    tmp_path: Path, stage: str, read_only: bool, error_kind: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    registered = "action_review_limit" if stage == "review" else "artifact_quota_exceeded"
    error = (
        RuntimeError(_payload())
        if error_kind == "runtime"
        else KernelError(
            registered if error_kind == "registered" else "secret_in_code_canary",
            _payload(),
            retryable=True,
        )
    )
    expected = (
        registered
        if error_kind == "registered" and stage != "context"
        else f"trusted_action_{stage}_failed"
    )
    gateway, actions, plans, audit, executor, callbacks = fault_gateway(
        root, stage, error, read_only=read_only
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理结束")])
    observer, exporter, reader = instrumented()
    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证回调故障", request_id="callback-error"
            )
            if stage == "output" and not read_only:
                assert turn.status is TurnStatus.WAITING_APPROVAL
                request = approval(turn)
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="boundary-test"
                    ),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                assert actions.status(request.plan_id).state == "succeeded"
            assert turn.status is (TurnStatus.FAILED if read_only else TurnStatus.INTERRUPTED)
            assert turn.error is not None
            assert turn.error.code == (expected if read_only else "uncertain_effect")
            assert not turn.error.retryable
            assert not pending_calls(turn)
            assert executor.calls == (1 if stage == "output" else 0)
            assert executor.reconciliations == 0
            assert len(provider.requests) == 1  # 失败关闭，未把内部异常送入下一次模型调用。
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert len(results) == 1
            if stage == "output":
                assert results[0].error is not None and results[0].error.code == expected
                assert not results[0].error.retryable
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == turn
            events = await store.events(thread.thread_id)
            assert replay(events) == await store.get_thread(thread.thread_id)
            if callbacks.route is not None:
                plan_id = callbacks.route.plan.execution.plan_id
                _assert_no_leak(
                    actions.status(plan_id).model_dump_json(),
                    *(event.model_dump_json() for event in actions.events(plan_id)),
                )
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
        assert_stores_safe(tmp_path)
    finally:
        observer.close()
        plans.close()
        audit.close()


def assert_stores_safe(root: Path) -> None:
    _assert_no_leak(*_store_bytes(*root.rglob("*.db*")))


async def assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader):
    """验证非空协议/遥测及实际模型请求，而不是仅检查sanitizer对象。"""

    assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == turn
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)
    client = AgentClient(
        InProcessAgentTransport(
            AgentProtocolServer(
                AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
            )
        )
    )
    try:
        await client.initialize()
        snapshot = await client.get_thread(thread.thread_id)
        protocol_events = await client.replay_events(thread.thread_id, limit=100)
        assert snapshot.latest_turn is not None and snapshot.latest_turn.status == turn.status.value
        assert protocol_events.scanned_through == snapshot.cursor
        _assert_no_leak(snapshot.model_dump_json(), protocol_events.model_dump_json())
    finally:
        await client.close()
    spans = exporter.get_finished_spans()
    assert any(span.name == "harnessix.agent.turn" for span in spans)
    data = reader.get_metrics_data()
    assert data is not None and data.resource_metrics
    _assert_no_leak(
        turn.model_dump_json(),
        *(event.model_dump_json() for event in events),
        *(request.model_dump_json() for request in provider.requests),
        *(span.to_json() for span in spans),
        data.to_json(),
    )
