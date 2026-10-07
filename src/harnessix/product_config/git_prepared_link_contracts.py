"""正式 prepared 关联：完整 Plan2 与原待审批请求的纯数据绑定，不签发认证或批准。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Self
from uuid import uuid5

from pydantic import ValidationInfo, field_validator, model_validator

from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.delivery.contracts import DeliveryContract
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryPlanV2
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot


class ProductGitPreparedLink(DeliveryContract):
    """只声明准备完成且尚待原审批；不扩展为后续执行或批准状态机。"""

    spec_version: Literal["harnessix.product-git-prepared-link/v1"] = (
        "harnessix.product-git-prepared-link/v1"
    )
    plan: ProductGitDeliveryPlanV2
    approval: TrustedActionApprovalRequestContent
    phase: Literal["prepared"] = "prepared"
    sequence: Literal[0] = 0

    @field_validator("sequence", mode="before")
    @classmethod
    def exact_sequence(cls, value: object) -> object:
        """Literal 的等值匹配不能把 False 或 0.0 规范化成正式序号。"""
        if type(value) is not int:
            raise ValueError("Git prepared关联序号必须为整数零")
        return value

    @model_validator(mode="after")
    def bound_prepared(self, info: ValidationInfo) -> Self:
        """复用原唯一严格快照，避免已建模型绕过深层类型和完整字段校验。"""
        if type(self) is not ProductGitPreparedLink:
            raise ValueError("Git prepared关联必须为正式契约实际类型")
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        plan = _snapshot(self.plan, ProductGitDeliveryPlanV2, checkpoint)
        approval = _snapshot(self.approval, TrustedActionApprovalRequestContent, checkpoint)
        route = plan.route
        execution = route.execution
        if (
            approval.diff_artifact != plan.review_artifact
            or approval.presentation != "patch_batch"
            or approval.decision is not None
            or approval.route_state != "pending_approval"
            or approval.call_id != plan.core.call.call_id
            or approval.plan_id != execution.plan_id
            or approval.approval_id
            != uuid5(execution.plan_id, "harnessix.agent-trusted-action-approval/v1")
            or approval.plan_fingerprint != route.fingerprint
            or approval.execution_fingerprint != execution.fingerprint
            or approval.policy_id != execution.policy.policy_id
            or approval.policy_version != execution.policy.version
        ):
            raise ValueError("Git prepared关联必须绑定同一完整Plan2与原待审批请求")
        # 原请求指纹依赖完整 Thread/Turn；此处只保留原 Revision 格式，不能另造算法。
        # 宿主须认证原 Session 后调用原 build_approval 重建请求并比较完整正文。
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "approval", approval)
        checkpoint()
        return self


def snapshot_product_git_prepared_link(
    value: object, *, checkpoint: Callable[[], None]
) -> ProductGitPreparedLink:
    """返回脱离全部可变别名的完整快照；成功仅表示声明满足契约。"""
    return _snapshot(value, ProductGitPreparedLink, checkpoint)
