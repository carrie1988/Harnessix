"""成功Provider正常返回故障数据进入实际Runtime、Session、Protocol与非空遥测。"""

from __future__ import annotations

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FixedOutput,
    build_gateway,
    descriptor,
)
from tests.trusted_actions.test_gateway_error_runtime import (
    assert_runtime_surfaces,
    assert_stores_safe,
)
from tests.trusted_actions.test_public_error_leakage import _payload
from tests.trusted_actions.test_success_projection_boundaries import output_reference


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("case", ["diagnostic", "limit"])
async def test_invalid_success_projection_is_not_published_or_sent_to_next_model(
    tmp_path, read_only, case
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded", output={"summary": "completed"}, artifact_sha256="b" * 64
        )
    )
    tool = descriptor()
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
    projected = {
        "summary": "completed",
        "artifact": output_reference("b" * 64),
        "diagnostic": _payload() if case == "diagnostic" else "x" * (1024 * 1024 + 1),
    }
    output = FixedOutput(projected)
    gateway, actions, plans, audit = build_gateway(
        root, executor, tool=tool, presentation="process", output=output
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理结束")])
    observer, exporter, reader = instrumented()
    expected = (
        "trusted_action_output_mismatch" if case == "diagnostic" else "trusted_action_output_limit"
    )
    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证成功投影边界", request_id="projection-success"
            )
            if not read_only:
                request = approval(turn)
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="projection-test"
                    ),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                assert actions.status(request.plan_id).state == "succeeded"
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert (
                len(results) == 1
                and results[0].error.code == expected
                and results[0].output is None
            )
            assert turn.status is (TurnStatus.FAILED if read_only else TurnStatus.INTERRUPTED)
            assert len(provider.requests) == 1 and output.calls == 2
            assert executor.calls == 1 and executor.reconciliations == 0
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
            assert (
                output.calls == 2
            )  # 首次失败后查询优先补偿一次；保存Tool Result后resume不重复发布。
        assert_stores_safe(tmp_path)
    finally:
        observer.close()
        plans.close()
        audit.close()
