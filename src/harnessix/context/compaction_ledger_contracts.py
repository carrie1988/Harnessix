"""模型Context规划：定义可恢复Compaction尝试账本合同。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.agent.errors import AgentFailure
from harnessix.agent.usage import (
    ModelAttempt,
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    TokenCount,
)
from harnessix.context.compaction_contracts import CompactionPlan, CompactionSummary
from harnessix.context.contracts import Digest
from harnessix.context.tool_result_contracts import ToolResultViewDecision
from harnessix.domain.models import ContractModel

COMPACTION_OPEN = frozenset({"planned", "sampling"})


class CompactionRecord(ContractModel):
    plan: CompactionPlan
    status: Literal["planned", "sampling", "summarized", "failed", "cancelled", "interrupted"]
    input_tokens_before: TokenCount
    output_tokens_before: TokenCount
    attempt: ModelAttempt | None = None
    summary: CompactionSummary | None = None
    candidate_history_sha256: Digest | None = None
    candidate_history_tokens: int | None = Field(default=None, ge=1, le=8_388_608, strict=True)
    failure: AgentFailure | None = None
    unaccounted_request_possible: bool = False
    created_event_sequence: int = Field(ge=2, strict=True)
    finished_event_sequence: int | None = Field(default=None, ge=3, strict=True)
    created_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        if self.attempt is not None and (
            self.attempt.step != self.plan.model_step or self.attempt.index != 1
        ):
            raise ValueError("摘要尝试必须绑定计划目标步骤且索引为1")
        if (self.status in COMPACTION_OPEN) != (self.finished_at is None):
            raise ValueError("压缩运行阶段与结束时间不一致")
        if (self.status in COMPACTION_OPEN) != (self.finished_event_sequence is None):
            raise ValueError("压缩运行阶段与结束事件序号不一致")
        if self.created_event_sequence != self.plan.source_event_sequence + 1:
            raise ValueError("压缩计划事件没有紧邻来源快照")
        if (
            self.finished_event_sequence is not None
            and self.finished_event_sequence <= self.created_event_sequence
        ):
            raise ValueError("压缩结束事件序号不晚于计划")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("压缩结束时间早于计划")
        if self.attempt is not None and (
            self.attempt.started_at < self.created_at
            or (
                self.attempt.finished_at is not None
                and (
                    self.attempt.finished_at < self.attempt.started_at
                    or (
                        self.finished_at is not None and self.finished_at < self.attempt.finished_at
                    )
                )
            )
        ):
            raise ValueError("摘要请求时间不属于压缩运行")
        if self.status == "planned" and self.attempt is not None:
            raise ValueError("未采样计划不可预置尝试")
        if self.status == "sampling" and self.attempt is None:
            raise ValueError("摘要采样缺少尝试")
        if self.status not in COMPACTION_OPEN and self.attempt is not None:
            if self.attempt.status == "running":
                raise ValueError("压缩终态之前必须结算请求")
        if self.status == "summarized":
            if (
                self.attempt is None
                or self.attempt.status != "completed"
                or self.summary is None
                or self.summary.compaction_id != self.plan.compaction_id
                or self.candidate_history_sha256 is None
                or self.candidate_history_tokens is None
                or self.failure is not None
            ):
                raise ValueError("已生成摘要缺少成功请求和候选证据")
        elif any(
            value is not None
            for value in (
                self.summary,
                self.candidate_history_sha256,
                self.candidate_history_tokens,
            )
        ):
            raise ValueError("非摘要完成状态不可预置候选")
        if (self.status in {"failed", "cancelled", "interrupted"}) != (self.failure is not None):
            raise ValueError("压缩失败阶段缺少一致的失败事实")
        if self.unaccounted_request_possible and (
            self.status not in {"failed", "interrupted"} or self.attempt is not None
        ):
            raise ValueError("未记账请求风险只能绑定无Attempt的失败或中断压缩")
        return self


class CompactionPlanned(ContractModel):
    type: Literal["compaction_planned"] = "compaction_planned"
    plan: CompactionPlan
    decisions: tuple[ToolResultViewDecision, ...] = Field(default_factory=tuple, max_length=8192)


class CompactionAttemptStarted(ContractModel):
    type: Literal["compaction_attempt_started"] = "compaction_attempt_started"
    compaction_id: UUID
    event: ModelAttemptStarted


class CompactionUsageObserved(ContractModel):
    type: Literal["compaction_usage_observed"] = "compaction_usage_observed"
    compaction_id: UUID
    event: ModelUsageObserved


class CompactionAttemptFinished(ContractModel):
    type: Literal["compaction_attempt_finished"] = "compaction_attempt_finished"
    compaction_id: UUID
    event: ModelAttemptFinished


class CompactionSummarized(ContractModel):
    type: Literal["compaction_summarized"] = "compaction_summarized"
    compaction_id: UUID
    summary: CompactionSummary
    candidate_history_sha256: Digest
    candidate_history_tokens: int = Field(ge=1, le=8_388_608, strict=True)

    @model_validator(mode="after")
    def matching_identity(self) -> Self:
        if self.summary.compaction_id != self.compaction_id:
            raise ValueError("摘要与压缩运行身份不一致")
        return self


class CompactionRejected(ContractModel):
    type: Literal["compaction_rejected"] = "compaction_rejected"
    compaction_id: UUID
    outcome: Literal["failed", "cancelled", "interrupted"]
    failure: AgentFailure
    unaccounted_request_possible: bool = False


CompactionEvent = (
    CompactionPlanned
    | CompactionAttemptStarted
    | CompactionUsageObserved
    | CompactionAttemptFinished
    | CompactionSummarized
    | CompactionRejected
)
