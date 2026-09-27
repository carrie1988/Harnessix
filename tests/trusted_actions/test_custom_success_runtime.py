"""custom正文授权通过真实Runtime、Model历史、Session、Protocol和非空遥测验证。"""

from __future__ import annotations

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.trusted_actions.test_agent_gateway import FakeExecutor, build_gateway, descriptor
from tests.trusted_actions.test_gateway_error_runtime import (
    assert_runtime_surfaces,
    assert_stores_safe,
)
from tests.trusted_actions.test_public_error_leakage import _payload


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("case", ["undeclared", "extra", "valid"])
async def test_custom_authorization_at_actual_public_surfaces(tmp_path, read_only, case):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    tool = descriptor()
    if case == "undeclared":
        tool = ToolDescriptor.model_validate(tool.model_dump(exclude={"public_output_schema"}))
    if read_only:
        tool = tool.model_copy(
            update={
                "effect_class": EffectClass.READ_ONLY,
                "risk_level": RiskLevel.LOW,
                "requires_idempotency": False,
                "requires_approval": False,
                "supports_reconciliation": False,
            }
        )
    body = {"summary": "completed"}
    if case != "valid":
        body["diagnostic"] = _payload()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=body))
    gateway, actions, plans, audit = build_gateway(root, executor, tool=tool)
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理结束")])
    observer, exporter, reader = instrumented()
    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证输出", request_id="custom-boundary"
            )
            if not read_only:
                request = approval(turn)
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="public-contract-test"
                    ),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert turn.status is (TurnStatus.COMPLETED if case == "valid" else TurnStatus.FAILED)
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert results and results[0].trusted_action.state == "succeeded"
            assert results[0].output == (body if case == "valid" else None)
            assert len(provider.requests) == (2 if case == "valid" else 1)
            assert executor.calls == 1 and executor.reconciliations == 0
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
        assert_stores_safe(tmp_path)
        assert (root / "file.txt").read_text() == "unchanged"
    finally:
        observer.close()
        audit.close()
        plans.close()
