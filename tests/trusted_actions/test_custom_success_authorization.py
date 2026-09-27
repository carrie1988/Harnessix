"""未声明公开合同的自定义成功正文不能因匹配审计Hash而公开。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.approvals import tool_fingerprint, trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FixedOutput,
    agent_state,
    build_gateway,
    descriptor,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload
from tests.trusted_actions.test_success_projection_boundaries import output_reference


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("owner", [True, False])
@pytest.mark.parametrize("case", ["diagnostic", "scalar"])
async def test_undeclared_custom_success_does_not_publish_body(
    tmp_path: Path, read_only, owner, case
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    tool = ToolDescriptor.model_validate(descriptor().model_dump(exclude={"public_output_schema"}))
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
    body = {"diagnostic": _payload()} if case == "diagnostic" else _payload()
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded", output=body, artifact_sha256="b" * 64 if owner else None
        )
    )
    provider = FixedOutput({"diagnostic": _payload(), "artifact": output_reference("b" * 64)})
    gateway, actions, plans, audit = build_gateway(
        root,
        executor,
        tool=tool,
        presentation="process" if owner else "tool",
        output=provider if owner else None,
    )
    try:
        thread, turn, call = agent_state(root)
        call = call.model_copy(
            update={
                "tool_version": tool.version,
                "tool_fingerprint": tool_fingerprint(tool),
                "effect_class": tool.effect_class,
                "requires_approval": tool.requires_approval,
            }
        )
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        if read_only:
            operation = gateway.prepare(thread, turn, call, CancelToken())
        else:
            request = await gateway.prepare(thread, turn, call, CancelToken())
            approved = gateway.decide(
                thread,
                turn,
                call,
                request,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="custom-contract-test"),
            )
            operation = gateway.execute(thread, turn, call, approved, CancelToken())
        with pytest.raises(KernelError) as caught:
            await operation
        assert caught.value.code == "trusted_action_output_mismatch"
        _assert_no_leak(str(caught.value))
        assert provider.calls == 0
        assert executor.calls == 1 and executor.reconciliations == 0
        assert actions.status(plan_id).state == "succeeded"
        events = actions.events(plan_id)
        assert events[-1].to_state == "succeeded"
        assert events[-1].output_sha256 == canonical_digest(body)
        assert (root / "file.txt").read_text() == "unchanged"
    finally:
        audit.close()
        plans.close()
