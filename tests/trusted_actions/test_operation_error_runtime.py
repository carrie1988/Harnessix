"""执行/对账抛出的异常经真实Runtime五公开面的效果语义与泄漏回归。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.trusted_actions.test_agent_gateway import FakeExecutor, FileInput
from tests.trusted_actions.test_gateway_error_boundaries import fault_gateway
from tests.trusted_actions.test_gateway_error_runtime import (
    assert_runtime_surfaces,
    assert_stores_safe,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload


class ThrowingExecutor(FakeExecutor):
    """只注入合成异常；写场景恢复只对账，绝不再次execute。"""

    def __init__(self, stage: str, error: Exception) -> None:
        super().__init__(ActionExecutionOutcome(kind="succeeded"))
        self.stage = stage
        self.error = error
        self.plan_id = None

    async def execute(self, plan, arguments: BaseModel) -> ActionExecutionOutcome:
        FileInput.model_validate(arguments)
        self.calls += 1
        self.plan_id = plan.execution.plan_id
        if self.stage == "execute":
            raise self.error
        return ActionExecutionOutcome(kind="unknown", error_code="write_effect_response_lost")

    async def reconcile(self, plan, arguments: BaseModel) -> ActionExecutionOutcome:
        FileInput.model_validate(arguments)
        self.reconciliations += 1
        self.plan_id = plan.execution.plan_id
        raise self.error


@pytest.mark.parametrize(
    "stage,read_only",
    [
        pytest.param("execute", True, id="read-execute"),
        pytest.param("execute", False, id="write-execute"),
        pytest.param("reconcile", False, id="write-reconcile"),
    ],
)
@pytest.mark.parametrize("error_kind", ["kernel", "runtime", "timeout"])
async def test_operation_exceptions_preserve_effects_without_leaking_diagnostics(
    tmp_path: Path, stage: str, read_only: bool, error_kind: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "file.txt"
    source.write_text("unchanged", encoding="utf-8")
    error = (
        KernelError("secret_in_code_canary", _payload(), retryable=True)
        if error_kind == "kernel"
        else TimeoutError(_payload())
        if error_kind == "timeout"
        else RuntimeError(_payload())
    )
    executor = ThrowingExecutor(stage, error)
    gateway, actions, plans, audit, _, callbacks = fault_gateway(
        root,
        stage,
        error,
        read_only=read_only,
        executor=executor,
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理了只读故障")])
    observer, exporter, reader = instrumented()
    expected = (
        ("executor_timeout" if error_kind == "timeout" else "executor_error")
        if read_only
        else (
            "write_effect_timeout_unknown" if error_kind == "timeout" else "unexpected_write_error"
        )
    )
    injected = False

    def fault(point: str) -> None:
        nonlocal injected
        if stage == "reconcile" and point == "runtime.after_tool" and not injected:
            injected = True
            # Router已保存UNKNOWN而Session尚未保存Result，恢复只能对账。
            raise RuntimeError("合成的Router与Session记账间隙")

    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer, fault=fault
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证异常分类", request_id="operation-error"
            )
            if not read_only:
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
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert len(results) == 1 and results[0].error is not None
            assert executor.calls == 1 and callbacks.calls == 0
            assert executor.plan_id is not None
            events = actions.events(executor.plan_id)
            if read_only:
                assert turn.status is TurnStatus.COMPLETED and results[0].outcome == "failed"
                assert results[0].error.code == expected and not results[0].error.retryable
                assert executor.reconciliations == 0 and len(provider.requests) == 2
                # 确实进入下一次模型历史，不能用一次请求的空结果面冒充验证。
                assert any(
                    isinstance(item.content, ToolResultContent) and item.content == results[0]
                    for item in provider.requests[-1].history
                )
                assert actions.status(executor.plan_id).state == "failed"
            else:
                assert turn.status is TurnStatus.INTERRUPTED and results[0].outcome == "unknown"
                assert executor.reconciliations == (1 if stage == "reconcile" else 0)
                assert len(provider.requests) == 1
                assert actions.status(executor.plan_id).state == "unknown"
                reconcile_code = (
                    "reconciliation_timeout" if error_kind == "timeout" else "reconciliation_error"
                )
                assert results[0].error.code == (
                    reconcile_code if stage == "reconcile" else expected
                )
                if stage == "execute":
                    assert any(
                        event.error_code == expected and event.to_state == "unknown"
                        for event in events
                    )
            _assert_no_leak(
                *(event.model_dump_json() for event in events),
                actions.status(executor.plan_id).model_dump_json(),
            )
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
        assert_stores_safe(tmp_path)
        assert source.read_text(encoding="utf-8") == "unchanged"
    finally:
        observer.close()
        plans.close()
        audit.close()
