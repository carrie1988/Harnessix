"""原始返回预算通过真实Runtime、模型历史、Session、Protocol与遥测验证公开边界。"""

from __future__ import annotations

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.trusted_actions.test_agent_gateway import FakeExecutor
from tests.trusted_actions.test_gateway_error_boundaries import fault_gateway
from tests.trusted_actions.test_gateway_error_runtime import (
    assert_runtime_surfaces,
    assert_stores_safe,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload


class ReturningRaw(FakeExecutor):
    def __init__(self, stage, reason):
        raw = ActionExecutionOutcome.model_construct(
            kind="succeeded",
            error_code="invalid" if reason == "invalid" else None,
            output={"diagnostic": _payload() + ("x" * (1024 * 1024) if reason == "limit" else "")},
            artifact_sha256="b" * 64,
        )
        super().__init__(
            raw
            if stage == "execute"
            else ActionExecutionOutcome(kind="unknown", error_code="executor_error"),
            raw if stage == "reconcile" else None,
        )
        self.plan_id = None

    async def execute(self, plan, arguments):
        self.plan_id = plan.execution.plan_id
        return await super().execute(plan, arguments)


@pytest.mark.parametrize(
    "stage,read_only", [("execute", True), ("execute", False), ("reconcile", False)]
)
@pytest.mark.parametrize("reason", ["limit", "invalid"])
async def test_raw_outcome_rejection_is_safe_on_every_actual_runtime_surface(
    tmp_path, stage, read_only, reason
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    executor = ReturningRaw(stage, reason)
    gateway, actions, plans, audit, _, callbacks = fault_gateway(
        root, stage, RuntimeError("unused"), read_only=read_only, executor=executor
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理结束")])
    observer, exporter, reader = instrumented()
    expected = (
        f"executor_output_{reason}"
        if read_only
        else f"write_output_{reason}_unknown"
        if stage == "execute"
        else f"reconciliation_output_{reason}"
    )
    injected = False

    def fault(point):
        nonlocal injected
        if stage == "reconcile" and point == "runtime.after_tool" and not injected:
            injected = True
            raise RuntimeError("合成的Router与Session记账间隙")

    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer, fault=fault
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证原始返回预算", request_id="raw-output-budget"
            )
            if not read_only:
                request = approval(turn)
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="raw-output-test"
                    ),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert (
                len(results) == 1
                and results[0].error.code == expected
                and results[0].output is None
            )
            assert not results[0].error.retryable
            assert turn.status is (TurnStatus.COMPLETED if read_only else TurnStatus.INTERRUPTED)
            assert len(provider.requests) == (2 if read_only else 1)
            if read_only:
                assert any(item.content == results[0] for item in provider.requests[-1].history)
            assert (
                executor.calls == 1
                and executor.reconciliations == (stage == "reconcile")
                and callbacks.calls == 0
            )
            assert actions.status(executor.plan_id).state == ("failed" if read_only else "unknown")
            assert all(operation.state == "completed" for operation in audit.operations())
            _assert_no_leak(
                *(event.model_dump_json() for event in actions.events(executor.plan_id))
            )
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
        assert_stores_safe(tmp_path)
        assert (root / "file.txt").read_text(encoding="utf-8") == "unchanged"
    finally:
        observer.close()
        plans.close()
        audit.close()
