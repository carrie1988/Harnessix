"""Backup v2 的 Git 业务快照合同；只保存已认证历史，不授予执行权限。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, ValidationInfo, model_validator

from harnessix.delivery.contracts import DeliveryContract
from harnessix.product_config.git_decision_link_contracts import (
    ProductGitApprovedLink,
    ProductGitCancelledLink,
    ProductGitDecisionLink,
    ProductGitDeniedLink,
)
from harnessix.product_config.git_prefix_catalog import GitPrefixCatalog
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink


class GitBusinessLinkSnapshot(DeliveryContract):
    """一条原 prepared 关联及其可选终态决定。"""

    prepared: ProductGitPreparedLink
    decision: ProductGitDecisionLink | None = None

    @model_validator(mode="after")
    def same_history(self, info: ValidationInfo) -> Self:
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        checkpoint()
        if type(self.prepared) is not ProductGitPreparedLink:
            raise ValueError("Git业务快照的prepared类型无效")
        if self.decision is not None:
            if type(self.decision) not in {
                ProductGitApprovedLink,
                ProductGitDeniedLink,
                ProductGitCancelledLink,
            } or (
                self.decision.plan != self.prepared.plan
                or self.decision.approval_request != self.prepared.approval
            ):
                raise ValueError("Git业务快照的决定不属于同一prepared历史")
        checkpoint()
        return self


class GitDeliveryBusinessSnapshot(DeliveryContract):
    """同一捕获窗口内的完整 GitDB 认证目录与当前已实现业务历史。"""

    spec_version: Literal["harnessix.git-delivery-business-snapshot/v1"] = (
        "harnessix.git-delivery-business-snapshot/v1"
    )
    capture_id: UUID
    prefix: GitPrefixCatalog
    links: tuple[GitBusinessLinkSnapshot, ...] = Field(max_length=100_000)

    @model_validator(mode="after")
    def complete_prefix_projection(self, info: ValidationInfo) -> Self:
        """当前 v1 只接受 product_link 全集；未知业务表必须在上游失败关闭。"""
        checkpoint = info.context["checkpoint"] if info.context else lambda: None
        routes = []
        expected_streams = []
        expected_revision = 0
        for link in self.links:
            checkpoint()
            core = link.prepared.plan.core
            route_id = link.prepared.plan.route.execution.plan_id
            routes.append(route_id.hex)
            expected_revision += 1 + int(link.decision is not None)
            expected_streams.append(
                (
                    "product_link",
                    route_id,
                    route_id,
                    core.delivery_id,
                    core.thread_id,
                    core.turn_id,
                    core.call.call_id,
                    1 + int(link.decision is not None),
                )
            )
        actual_streams = [
            (
                stream.first.record_kind,
                stream.first.record_id,
                stream.first.route_id,
                stream.first.delivery_id,
                stream.first.thread_id,
                stream.first.turn_id,
                stream.first.call_id,
                stream.count,
            )
            for stream in self.prefix.streams
        ]
        if (
            routes != sorted(set(routes))
            or expected_revision != self.prefix.revision
            or actual_streams != expected_streams
        ):
            raise ValueError("Git业务快照没有完整对应认证前缀")
        checkpoint()
        return self
