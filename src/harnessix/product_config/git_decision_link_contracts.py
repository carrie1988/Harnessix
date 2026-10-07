"""Git 决定的闭合数据声明；事件索引和指纹不是来源认证或执行授权。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator, model_validator

from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.delivery.contracts import DeliveryContract
from harnessix.domain.models import ApprovalOutcome
from harnessix.execution.contracts import ExecutionApprovalCheckpoint
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryPlanV2
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot, invalid_git_delivery_plan
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.tools.contracts import Revision


class GitSessionEventRef(DeliveryContract):
    """原 Session 事件的定位声明；不保存事件副本，也不验证 MAC 或 Thread。"""

    event_id: UUID
    sequence: int = Field(ge=1)
    digest: Revision


def _ordered_events(events: tuple[GitSessionEventRef, ...]) -> None:
    """只校验声明的唯一定位及严格先后；真实历史仍须由原 Session 证明。"""
    if len({event.event_id for event in events}) != len(events) or any(
        before.sequence >= after.sequence for before, after in zip(events, events[1:], strict=False)
    ):
        raise ValueError("Git 决定事件定位必须唯一且严格有序")


class _DecisionLink(DeliveryContract):
    """三种封套共用的完整前驱和原执行检查点，不接受任意状态组合。"""

    spec_version: Literal["harnessix.product-git-decision-link/v1"] = (
        "harnessix.product-git-decision-link/v1"
    )
    sequence: Literal[1] = 1
    plan: ProductGitDeliveryPlanV2
    approval_request: TrustedActionApprovalRequestContent
    request_event: GitSessionEventRef
    prepared_body_sha256: Revision
    router_approval: ExecutionApprovalCheckpoint
    route_decision_sequence: int = Field(ge=1)
    route_decision_digest: Revision

    @field_validator("sequence", mode="before")
    @classmethod
    def exact_sequence(cls, value: object) -> object:
        """拒绝 bool/float 的 Literal 等值归一化，决定序号只能是整数一。"""
        if type(value) is not int:
            raise ValueError("Git 决定关联序号必须为整数一")
        return value

    @model_validator(mode="after")
    def complete_predecessor(self, info: ValidationInfo) -> Self:
        """沿原 prepared 校验重建前驱；原 Session 与执行指纹域不能混用。"""
        if type(self) not in {
            ProductGitApprovedLink,
            ProductGitDeniedLink,
            ProductGitCancelledLink,
        }:
            raise ValueError("Git 决定必须为正式闭合变体实际类型")
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        predecessor = ProductGitPreparedLink.model_validate(
            {"plan": self.plan, "approval": self.approval_request},
            context={"checkpoint": checkpoint},
        )
        approval = _snapshot(self.router_approval, ExecutionApprovalCheckpoint, checkpoint)
        request_event = _snapshot(self.request_event, GitSessionEventRef, checkpoint)
        execution = predecessor.plan.route.execution
        if (
            approval.plan_id != execution.plan_id
            or approval.plan_fingerprint != execution.fingerprint
            or approval.decision.decided_at.utcoffset() is None
        ):
            raise ValueError("Git 决定检查点必须绑定原完整执行计划与有时区的决定时间")
        object.__setattr__(self, "plan", predecessor.plan)
        object.__setattr__(self, "approval_request", predecessor.approval)
        object.__setattr__(self, "router_approval", approval)
        object.__setattr__(self, "request_event", request_event)
        checkpoint()
        return self


class _HumanDecisionLink(_DecisionLink):
    """保存两域原决定正文；相等判断不替代认证历史及期限检查。"""

    session_decision: TrustedActionApprovalRequestContent
    decision_event: GitSessionEventRef

    @model_validator(mode="after")
    def same_original_decision(self, info: ValidationInfo) -> Self:
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        content = _snapshot(self.session_decision, TrustedActionApprovalRequestContent, checkpoint)
        event = _snapshot(self.decision_event, GitSessionEventRef, checkpoint)
        record = content.decision
        expected = (
            ApprovalOutcome.APPROVED
            if type(self) is ProductGitApprovedLink
            else ApprovalOutcome.REJECTED
        )
        execution_decision = self.router_approval.decision
        # 只有原 Reducer 改变的两字段可不同；不另造请求指纹算法或精简比较键。
        pending = content.model_copy(update={"decision": None, "route_state": "pending_approval"})
        if (
            record is None
            or pending != self.approval_request
            or record.outcome is not expected
            or content.route_state
            != ("ready" if expected is ApprovalOutcome.APPROVED else "denied")
            or record.request_fingerprint != self.approval_request.request_fingerprint
            or execution_decision.outcome is not record.outcome
            or execution_decision.actor != record.actor
            or execution_decision.reason != record.reason
            or execution_decision.decided_at != record.decided_at
            or record.decided_at.utcoffset() is None
        ):
            raise ValueError("Git 决定必须保留同一原请求及两域一致的决定正文")
        _ordered_events((self.request_event, event))
        object.__setattr__(self, "session_decision", content)
        object.__setattr__(self, "decision_event", event)
        checkpoint()
        return self


class ProductGitApprovedLink(_HumanDecisionLink):
    """人工批准事实声明；不能仅凭本对象继续 Git 写入。"""

    fact_kind: Literal["approved"] = "approved"
    phase: Literal["approved"] = "approved"


class ProductGitDeniedLink(_HumanDecisionLink):
    """人工拒绝声明；failed 仅为既有 GitDB 的负向存储投影。"""

    fact_kind: Literal["denied"] = "denied"
    phase: Literal["failed"] = "failed"


class ProductGitCancelledLink(_DecisionLink):
    """审批前已结算取消的声明，禁止伪造 Session 人工决定字段。"""

    fact_kind: Literal["cancelled"] = "cancelled"
    phase: Literal["failed"] = "failed"
    cancel_event: GitSessionEventRef
    approval_cancel_event: GitSessionEventRef
    call_result_event: GitSessionEventRef
    turn_terminal_event: GitSessionEventRef

    @model_validator(mode="after")
    def settled_cancel_shape(self, info: ValidationInfo) -> Self:
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        decision = self.router_approval.decision
        if (
            decision.outcome is not ApprovalOutcome.REJECTED
            or decision.actor != "system.cancel"
            or decision.reason != "turn_cancelled"
        ):
            raise ValueError("Git 取消声明必须保留原系统拒绝检查点")
        names = (
            "cancel_event",
            "approval_cancel_event",
            "call_result_event",
            "turn_terminal_event",
        )
        events = tuple(
            _snapshot(getattr(self, name), GitSessionEventRef, checkpoint) for name in names
        )
        _ordered_events((self.request_event, *events))
        for name, event in zip(names, events, strict=True):
            object.__setattr__(self, name, event)
        checkpoint()
        return self


ProductGitDecisionLink = Annotated[
    ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink,
    Field(discriminator="fact_kind"),
]


def snapshot_product_git_decision_link(
    value: object, *, checkpoint: Callable[[], None]
) -> ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink:
    """按确切变体深层重建，返回无可变别名的声明，不验真来源。"""
    checkpoint()
    kind = type(value)
    if kind is ProductGitApprovedLink:
        return _snapshot(value, ProductGitApprovedLink, checkpoint)
    if kind is ProductGitDeniedLink:
        return _snapshot(value, ProductGitDeniedLink, checkpoint)
    if kind is ProductGitCancelledLink:
        return _snapshot(value, ProductGitCancelledLink, checkpoint)
    raise invalid_git_delivery_plan()
