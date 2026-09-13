"""Trusted Action在Agent reducer中的纯校验规则。"""

from __future__ import annotations

from harnessix.agent.approvals import approval_for, trusted_action_invocation_id
from harnessix.agent.models import (
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer_support import require


def validate_trusted_action_effect(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    result: ToolResultContent,
) -> None:
    """绑定调用、Router计划、可选审批和效果来源。"""

    effect = result.trusted_action
    if effect is None:
        return
    require(
        effect.plan_id == trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        and result.action_id == effect.plan_id,
        "Trusted Action效果身份与调用不一致",
    )
    approval = approval_for(turn, call)
    if approval is not None:
        require(
            isinstance(approval.content, TrustedActionApprovalRequestContent)
            and approval.status == ItemStatus.COMPLETED
            and approval.content.plan_id == effect.plan_id
            and approval.content.plan_fingerprint == effect.plan_fingerprint,
            "Trusted Action效果与审批计划不匹配",
        )
    require(
        effect.origin == "recovery" or turn.status == TurnStatus.EXECUTING_TOOLS,
        "Trusted Action执行效果只能在执行状态发布",
    )
