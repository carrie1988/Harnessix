from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.context.contracts import Digest
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.domain.models import ContractModel


class CompactionPolicy(ContractModel):
    """窗口规划预算，不是开启自动压缩或发起付费请求的配置。"""

    spec_version: Literal["harnessix.compaction-policy/v1"] = "harnessix.compaction-policy/v1"
    target_history_tokens: int = Field(ge=512, le=8_388_608, strict=True)
    summary_reserve_tokens: int = Field(default=4096, ge=256, le=1_000_000, strict=True)
    max_summary_input_tokens: int = Field(ge=512, le=8_388_608, strict=True)
    retain_recent_groups: int = Field(default=2, ge=1, le=8192, strict=True)
    min_savings_tokens: int = Field(default=256, ge=1, le=8_388_608, strict=True)

    @model_validator(mode="after")
    def leave_retained_budget(self) -> Self:
        if self.summary_reserve_tokens >= self.target_history_tokens:
            raise ValueError("摘要预留必须小于目标历史预算")
        return self


class CompactionAnchor(ContractModel):
    """宿主固定的原始Item指纹；保留时扩展到完整闭合组。"""

    item_id: UUID
    source_sha256: Digest


class CompactionPlan(ContractModel):
    """无副作用的首窗口选择证据；不能作为活动窗口或执行授权。"""

    spec_version: Literal["harnessix.compaction-plan/v1"] = "harnessix.compaction-plan/v1"
    strategy: Literal["closed-prefix-with-pins/v1"] = "closed-prefix-with-pins/v1"
    estimator: Literal["utf8-bytes/v1"] = "utf8-bytes/v1"
    compaction_id: UUID
    thread_id: UUID
    turn_id: UUID
    source_event_sequence: int = Field(ge=1, strict=True)
    model_step: int = Field(ge=1, le=1000, strict=True)
    policy: CompactionPolicy
    tool_result_view_policy: ToolResultViewPolicy
    anchors: tuple[CompactionAnchor, ...] = Field(default_factory=tuple, max_length=64)
    source_item_ids: tuple[UUID, ...] = Field(min_length=2, max_length=8192)
    covered_item_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8192)
    retained_item_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8192)
    pinned_item_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8192)
    source_history_sha256: Digest
    model_history_sha256: Digest
    decisions_sha256: Digest
    summary_source_sha256: Digest
    retained_history_sha256: Digest
    source_history_tokens: int = Field(ge=1, le=8_388_608, strict=True)
    retained_history_tokens: int = Field(ge=1, le=8_388_608, strict=True)
    summary_input_tokens: int = Field(ge=1, le=8_388_608, strict=True)

    @model_validator(mode="after")
    def partition_and_budgets(self) -> Self:
        source = set(self.source_item_ids)
        covered = set(self.covered_item_ids)
        retained = set(self.retained_item_ids)
        pinned = set(self.pinned_item_ids)
        for ids in (
            self.source_item_ids,
            self.covered_item_ids,
            self.retained_item_ids,
            self.pinned_item_ids,
        ):
            if len(set(ids)) != len(ids):
                raise ValueError("压缩计划Item身份重复")
        if covered & retained or covered | retained != source or not pinned <= retained:
            raise ValueError("压缩覆盖集、保留集和固定集不能构成来源分区")
        for ids in (self.covered_item_ids, self.retained_item_ids, self.pinned_item_ids):
            selected = set(ids)
            if tuple(item_id for item_id in self.source_item_ids if item_id in selected) != ids:
                raise ValueError("压缩分区必须维持原历史顺序")
        anchor_ids = [anchor.item_id for anchor in self.anchors]
        if len(set(anchor_ids)) != len(anchor_ids) or not set(anchor_ids) <= pinned:
            raise ValueError("压缩锚点重复或未固定保留")
        reserved = self.retained_history_tokens + self.policy.summary_reserve_tokens
        if reserved > self.policy.target_history_tokens:
            raise ValueError("保留历史与摘要预留超过目标预算")
        if self.source_history_tokens - reserved < self.policy.min_savings_tokens:
            raise ValueError("压缩计划没有达到最小缩减量")
        if self.summary_input_tokens > self.policy.max_summary_input_tokens:
            raise ValueError("摘要来源超过输入预算")
        return self


class CompactionSummary(ContractModel):
    """待验证的低信任摘要正文；不包含工具、审批或窗口发布权限。"""

    spec_version: Literal["harnessix.compaction-summary/v1"] = "harnessix.compaction-summary/v1"
    compaction_id: UUID
    text: str = Field(min_length=1, max_length=1_000_000)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("摘要不允许空白或NUL")
        try:
            value.encode()
        except UnicodeEncodeError:
            raise ValueError("摘要必须是有效UTF-8文本") from None
        return value
