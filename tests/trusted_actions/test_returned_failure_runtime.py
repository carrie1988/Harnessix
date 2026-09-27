"""正常返回失败进入真实Runtime后的公开面和崩溃窗口恢复验证。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

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


class ReturningFailure(FakeExecutor):
    """返回正常数据而不抛异常，允许在真实记账间隙验证Reconcile。"""

    def __init__(self, stage: str, kind: str, code: str) -> None:
        hostile = ActionExecutionOutcome(
            kind=kind, error_code=code, output={"diagnostic": _payload()}, artifact_sha256="b" * 64
        )
        super().__init__(
            hostile
            if stage == "execute"
            else ActionExecutionOutcome(kind="unknown", error_code="lost"),
            hostile if stage == "reconcile" else None,
        )
        self.plan_id = None

    async def execute(self, plan, arguments: BaseModel) -> ActionExecutionOutcome:
        self.plan_id = plan.execution.plan_id
        FileInput.model_validate(arguments)
        return await super().execute(plan, arguments)


@pytest.mark.parametrize(
    "stage,kind,read_only",
    [
        ("execute", "failed", True),
        ("execute", "failed", False),
        ("execute", "unknown", False),
        ("reconcile", "failed", False),
        ("reconcile", "unknown", False),
        ("reconcile", "manual_intervention", False),
    ],
)
@pytest.mark.parametrize("code", ["secret_in_code_canary", "process_timeout", "executor_error"])
async def test_returned_failure_is_safe_on_every_actual_runtime_surface(
    tmp_path: Path, stage: str, kind: str, read_only: bool, code: str
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    executor = ReturningFailure(stage, kind, code)
    gateway, actions, plans, audit, _, callbacks = fault_gateway(
        root, "execute", RuntimeError("unused"), read_only=read_only, executor=executor
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理结束")])
    observer, exporter, reader = instrumented()
    injected = False

    def fault(point: str) -> None:
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
                thread.thread_id, "验证正常返回的失败", request_id="returned-failure"
            )
            if not read_only:
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
            expected = (
                code
                if code == "executor_error"
                else {
                    "failed": "action_failed",
                    "unknown": "action_effect_unknown",
                    "manual_intervention": "action_manual_intervention",
                }[kind]
            )
            assert results[0].error.code == expected and results[0].output is None
            assert results[0].trusted_action.state == kind
            assert not results[0].error.retryable
            assert turn.status is (
                (TurnStatus.FAILED if stage == "reconcile" else TurnStatus.COMPLETED)
                if kind == "failed"
                else TurnStatus.INTERRUPTED
            )
            assert len(provider.requests) == (2 if kind == "failed" and stage == "execute" else 1)
            assert executor.calls == 1 and executor.reconciliations == (stage == "reconcile")
            assert callbacks.calls == 0
            assert executor.plan_id is not None
            assert actions.status(executor.plan_id).state == kind
            _assert_no_leak(
                *(event.model_dump_json() for event in actions.events(executor.plan_id))
            )
            if kind == "failed" and stage == "execute":
                assert any(
                    isinstance(item.content, ToolResultContent) and item.content == results[0]
                    for item in provider.requests[-1].history
                )
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
        assert_stores_safe(tmp_path)
    finally:
        observer.close()
        plans.close()
        audit.close()
