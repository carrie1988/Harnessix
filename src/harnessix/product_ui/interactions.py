"""产品领域交互的冻结身份、公开投影与纯校验合同。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.projection import ProductViewState
from harnessix.protocol.contracts import (
    PublicApprovalRequestContent,
    PublicArtifactRef,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicToolCallContent,
)

ACTIVE_TURN_STATES: Final = frozenset(
    {
        "accepted",
        "preparing_context",
        "calling_model",
        "executing_tools",
        "waiting_approval",
        "waiting_action",
        "waiting_input",
        "finalizing",
        "cancelling",
    }
)
_CANCELLABLE_TURN_STATES: Final = ACTIVE_TURN_STATES - {"cancelling"}
_STEERABLE_TURN_STATES: Final = frozenset(
    {
        "accepted",
        "preparing_context",
        "calling_model",
        "executing_tools",
        "waiting_approval",
        "waiting_action",
        "waiting_input",
    }
)


@dataclass(frozen=True, slots=True)
class TurnBinding:
    """把Turn控制动作绑定到唯一Thread与Turn。"""

    thread_id: UUID
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class ApprovalBinding(TurnBinding):
    """审批提交所需的完整安全身份。"""

    call_id: UUID
    approval_id: UUID
    fingerprint: str


@dataclass(frozen=True, slots=True)
class QuestionBinding(TurnBinding):
    """问题回答所需的完整交互身份。"""

    call_id: UUID
    question_id: UUID


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """由当前公开Item唯一推导出的待决审批。"""

    binding: ApprovalBinding
    approval_type: Literal["tool", "patch", "patch_batch", "process"]
    policy_version: str
    tool: str
    tool_version: str
    effect_class: Literal["read_only", "idempotent_write", "non_idempotent_write", "destructive"]
    arguments_json: str
    diff_artifact: PublicArtifactRef | None


@dataclass(frozen=True, slots=True)
class PendingQuestion:
    """由当前公开Item唯一推导出的待回答问题。"""

    binding: QuestionBinding
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActiveTurnControl:
    """当前Turn可执行的取消与Steer能力。"""

    binding: TurnBinding
    status: str
    can_cancel: bool
    can_steer: bool


@dataclass(frozen=True, slots=True)
class UsageCostView:
    """公开Token用量与明确未知的费用语义。"""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    max_tokens: int | None
    cost_status: Literal["unknown"] = "unknown"
    cost_reason: Literal["price_not_exposed"] = "price_not_exposed"


class ApprovalEvidenceStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    INLINE = "inline"
    REQUIRED = "required"
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    """只在内存保存并与审批完整身份绑定的证据。"""

    binding: ApprovalBinding
    status: ApprovalEvidenceStatus
    artifact_id: UUID | None = None
    text: str = ""
    sha256: str | None = None
    records: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalReview:
    """审批展示事实及其批准准入结论。"""

    pending: PendingApproval
    evidence: ApprovalEvidence
    approve_allowed: bool


@dataclass(frozen=True, slots=True)
class InteractionSnapshot:
    """单个产品快照中可见的全部领域交互。"""

    approval: PendingApproval | None
    question: PendingQuestion | None
    turn_control: ActiveTurnControl | None
    usage_cost: UsageCostView | None
    approval_evidence: ApprovalEvidence | None


class InteractionIntent:
    """供Controller运行时识别的领域交互Intent标记。"""


@dataclass(frozen=True, slots=True)
class LoadApprovalEvidenceIntent(InteractionIntent):
    """为当前审批读取只读证据。"""

    binding: ApprovalBinding


@dataclass(frozen=True, slots=True)
class RespondApprovalIntent(InteractionIntent):
    """提交一次批准或拒绝决定。"""

    binding: ApprovalBinding
    outcome: Literal["approved", "rejected"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RespondQuestionIntent(InteractionIntent):
    """回答当前Turn正在等待的问题。"""

    binding: QuestionBinding
    answer: str


@dataclass(frozen=True, slots=True)
class CancelTurnIntent(InteractionIntent):
    """显式取消绑定的活动Turn。"""

    binding: TurnBinding


@dataclass(frozen=True, slots=True)
class SteerTurnIntent(InteractionIntent):
    """向绑定的活动Turn追加用户要求。"""

    binding: TurnBinding
    text: str


def _current_items(view: ProductViewState) -> tuple[object, ...]:
    turn = view.current_turn
    if turn is None:
        return ()
    return tuple(entry.item.content for entry in view.items if entry.turn_id == turn.turn_id)


def pending_approval(view: ProductViewState | None) -> PendingApproval | None:
    """严格推导唯一待审批事实，歧义状态失败关闭。"""

    if view is None or view.current_turn is None or view.current_turn.status != "waiting_approval":
        return None
    contents = _current_items(view)
    approvals = tuple(
        value
        for value in contents
        if isinstance(value, PublicApprovalRequestContent) and value.decision is None
    )
    if len(approvals) != 1:
        raise ProductUIError("interaction_state_invalid", "当前Turn的待审批事实不唯一")
    approval = approvals[0]
    calls = tuple(
        value
        for value in contents
        if isinstance(value, PublicToolCallContent) and value.call_id == approval.call_id
    )
    if len(calls) != 1:
        raise ProductUIError("interaction_state_invalid", "审批与公开Tool Call无法唯一关联")
    call = calls[0]
    return PendingApproval(
        binding=ApprovalBinding(
            view.thread.thread_id,
            view.current_turn.turn_id,
            call.call_id,
            approval.approval_id,
            approval.request_fingerprint,
        ),
        approval_type=approval.approval_type,
        policy_version=approval.policy_version,
        tool=call.tool,
        tool_version=call.tool_version,
        effect_class=call.effect_class,
        arguments_json=json.dumps(
            call.arguments,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ),
        diff_artifact=approval.diff_artifact,
    )


def pending_question(view: ProductViewState | None) -> PendingQuestion | None:
    """严格推导唯一未回答问题，歧义状态失败关闭。"""

    if view is None or view.current_turn is None or view.current_turn.status != "waiting_input":
        return None
    contents = _current_items(view)
    answered = {
        (value.question_id, value.call_id)
        for value in contents
        if isinstance(value, PublicQuestionAnswerContent)
    }
    questions = tuple(
        value
        for value in contents
        if isinstance(value, PublicQuestionRequestContent)
        and (value.question_id, value.call_id) not in answered
    )
    if len(questions) != 1:
        raise ProductUIError("interaction_state_invalid", "当前Turn的待回答问题不唯一")
    question = questions[0]
    return PendingQuestion(
        binding=QuestionBinding(
            view.thread.thread_id,
            view.current_turn.turn_id,
            question.call_id,
            question.question_id,
        ),
        question=question.question,
        options=question.options,
    )


def active_turn_control(view: ProductViewState | None) -> ActiveTurnControl | None:
    """按Runtime显式状态白名单投影Turn控制能力。"""

    if view is None or view.current_turn is None:
        return None
    turn = view.current_turn
    return ActiveTurnControl(
        binding=TurnBinding(view.thread.thread_id, turn.turn_id),
        status=turn.status,
        can_cancel=turn.status in _CANCELLABLE_TURN_STATES,
        can_steer=turn.status in _STEERABLE_TURN_STATES,
    )


def usage_cost_view(view: ProductViewState | None) -> UsageCostView | None:
    """投影公开Token用量，费用保持不可推断的未知状态。"""

    if view is None or view.current_turn is None:
        return None
    usage = view.current_turn.usage
    return UsageCostView(
        input_tokens=0 if usage is None else usage.input_tokens,
        output_tokens=0 if usage is None else usage.output_tokens,
        total_tokens=0 if usage is None else usage.total_tokens,
        max_tokens=None
        if view.current_turn.budget is None
        else view.current_turn.budget.max_tokens,
    )


def default_approval_evidence(pending: PendingApproval) -> ApprovalEvidence:
    """按审批类型生成尚未执行I/O的默认证据状态。"""

    reference = pending.diff_artifact
    if reference is not None:
        return ApprovalEvidence(
            pending.binding,
            ApprovalEvidenceStatus.REQUIRED,
            artifact_id=reference.artifact_id,
        )
    if pending.approval_type == "patch":
        return ApprovalEvidence(
            pending.binding,
            ApprovalEvidenceStatus.INLINE,
            text=pending.arguments_json,
        )
    if pending.approval_type == "patch_batch":
        return ApprovalEvidence(
            pending.binding,
            ApprovalEvidenceStatus.UNAVAILABLE,
            error_code="diff_unavailable",
        )
    return ApprovalEvidence(pending.binding, ApprovalEvidenceStatus.NOT_REQUIRED)


def retained_approval_evidence(
    view: ProductViewState | None,
    evidence: ApprovalEvidence | None,
) -> ApprovalEvidence | None:
    """只保留与当前完整审批身份逐字段相等的内存证据。"""

    if evidence is None:
        return None
    try:
        pending = pending_approval(view)
    except ProductUIError:
        return None
    return evidence if pending is not None and pending.binding == evidence.binding else None


def interaction_snapshot(
    view: ProductViewState | None,
    evidence: ApprovalEvidence | None = None,
) -> InteractionSnapshot:
    """从公开产品视图纯函数生成领域交互快照。"""

    approval = pending_approval(view)
    retained = retained_approval_evidence(view, evidence)
    return InteractionSnapshot(
        approval=approval,
        question=pending_question(view),
        turn_control=active_turn_control(view),
        usage_cost=usage_cost_view(view),
        approval_evidence=retained,
    )


def approval_review(
    view: ProductViewState | None,
    evidence: ApprovalEvidence | None,
) -> ApprovalReview:
    """生成审批窗口模型并计算是否允许批准。"""

    pending = pending_approval(view)
    if pending is None:
        raise ProductUIError("approval_stale", "当前没有可处理的审批")
    selected = retained_approval_evidence(view, evidence) or default_approval_evidence(pending)
    return ApprovalReview(
        pending=pending,
        evidence=selected,
        approve_allowed=selected.status
        in {
            ApprovalEvidenceStatus.NOT_REQUIRED,
            ApprovalEvidenceStatus.INLINE,
            ApprovalEvidenceStatus.READY,
        },
    )
