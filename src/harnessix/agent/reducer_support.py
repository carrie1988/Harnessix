"""保存Reducer共用查询和Process Action一致性守卫；不执行状态提交或外部副作用。"""

from __future__ import annotations

from uuid import UUID

from harnessix.agent.approvals import approval_for, approval_matches
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    PROCESS_RESOLVED_STATUSES,
    Item,
    ItemStatus,
    ProcessActionEffect,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.domain.models import (
    ALLOWED_ACTION_TRANSITIONS,
    ActionStatus,
    ApprovalOutcome,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise KernelError("invalid_event", message)


def get_turn(thread: Thread, turn_id: UUID) -> Turn:
    for turn in thread.turns:
        if turn.turn_id == turn_id:
            return turn
    raise KernelError("turn_not_found", "Turn 不存在")


def pending_calls(turn: Turn) -> list[ToolCallContent]:
    settled = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, ToolResultContent) and item.status != ItemStatus.STARTED
    }
    return [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
        and item.status == ItemStatus.COMPLETED
        and item.content.call_id not in settled
    ]


def _process_approval(turn: Turn, call: ToolCallContent) -> Item | None:
    item = approval_for(turn, call)
    return (
        item
        if item is not None and isinstance(item.content, ProcessApprovalRequestContent)
        else None
    )


def _process_effects(turn: Turn, call: ToolCallContent) -> list[ProcessActionEffect]:
    return [
        item.content.effect
        for item in turn.items
        if isinstance(item.content, ProcessActionStateContent)
        and item.content.call_id == call.call_id
        and item.status == ItemStatus.COMPLETED
    ]


def _action_status_reachable(source: ActionStatus, target: ActionStatus) -> bool:
    pending = list(ALLOWED_ACTION_TRANSITIONS.get(source, frozenset()))
    visited = set(pending)
    while pending:
        current = pending.pop()
        if current == target:
            return True
        for following in ALLOWED_ACTION_TRANSITIONS.get(current, frozenset()):
            if following not in visited:
                visited.add(following)
                pending.append(following)
    return False


def _validate_process_state(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    content: ProcessActionStateContent,
) -> None:
    approval = _process_approval(turn, call)
    require(
        turn.status == TurnStatus.WAITING_ACTION
        and approval is not None
        and approval.status == ItemStatus.COMPLETED,
        "Process状态只能观察已决定的等待Action",
    )
    assert approval is not None and isinstance(approval.content, ProcessApprovalRequestContent)
    projection = approval.content
    require(
        projection.decision is not None and approval_matches(thread, turn, call, projection),
        "Process状态缺少匹配的Action审批投影",
    )
    effect = content.effect
    require(
        content.call_id == call.call_id
        and (
            effect.plan_fingerprint,
            effect.action_id,
            effect.action_fingerprint,
        )
        == (
            projection.plan.approval_fingerprint,
            projection.plan.action_id,
            projection.plan.action_fingerprint,
        ),
        "Process状态与调用计划不匹配",
    )
    assert projection.decision is not None
    require(
        (projection.decision.outcome == ApprovalOutcome.REJECTED)
        == (effect.status is ActionStatus.DENIED),
        "Process状态与Action审批决定不一致",
    )
    previous = _process_effects(turn, call)
    require(
        not previous or previous[-1].status not in PROCESS_RESOLVED_STATUSES,
        "Process终止状态不可追加观察",
    )
    source = previous[-1].status if previous else projection.action_status
    require(
        _action_status_reachable(source, effect.status)
        or (
            not previous and source == effect.status and effect.status in PROCESS_RESOLVED_STATUSES
        ),
        "Process状态观察倒退、重复或不属于原Action生命周期",
    )


def _validate_process_result(turn: Turn, call: ToolCallContent, content: ToolResultContent) -> None:
    approval = _process_approval(turn, call)
    if approval is None and content.process is None:
        return
    if approval is not None and turn.status == TurnStatus.CANCELLING and content.process is None:
        assert isinstance(approval.content, ProcessApprovalRequestContent)
        require(
            content.outcome == "unknown"
            and content.action_id == approval.content.plan.action_id
            and content.error is not None
            and content.error.code == "uncertain_effect"
            and content.output is None
            and content.patch is None
            and content.patch_batch is None
            and content.diff_artifact is None,
            "取消Process等待必须保留原Action身份并保守标记未知效果",
        )
        return
    require(
        approval is not None
        and approval.status == ItemStatus.COMPLETED
        and isinstance(approval.content, ProcessApprovalRequestContent)
        and approval.content.decision is not None,
        "Process结果缺少已决定的Action审批投影",
    )
    effects = _process_effects(turn, call)
    require(
        bool(effects) and effects[-1].status in PROCESS_RESOLVED_STATUSES, "Process结果缺少终止观察"
    )
    require(content.process is not None, "Process结果缺少Action终止证据")
    assert effects and content.process is not None
    require(
        content.process == effects[-1] and content.action_id == effects[-1].action_id,
        "Process结果与Action终止观察不一致",
    )
    expected = {
        "denied": "failed",
        "succeeded": "succeeded",
        "failed": "failed",
        "unknown": "unknown",
        "manual_intervention": "unknown",
    }[effects[-1].status.value]
    require(content.outcome == expected, "Process结果结论与Action状态不一致")
