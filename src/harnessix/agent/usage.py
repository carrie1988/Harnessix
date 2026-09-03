from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.agent.billing import ResponseBillingMetadata
from harnessix.agent.errors import AgentFailure
from harnessix.domain.models import ContractModel

TokenCount = Annotated[int, Field(ge=0, strict=True)]
ModelIdentifier = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
]
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
)


class UsageObservation(ContractModel):
    """一次尝试的累计观测；输入含缓存、输出含推理，不是本次增量。"""

    completeness: Literal["unknown", "partial", "complete"] = "unknown"
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    uncached_input_tokens: TokenCount | None = None
    cache_read_input_tokens: TokenCount | None = None
    cache_creation_input_tokens: TokenCount | None = None
    reasoning_output_tokens: TokenCount | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        known = any(getattr(self, field) is not None for field in TOKEN_FIELDS)
        if self.completeness == "unknown" and known:
            raise ValueError("未知用量不得填零或其他计数")
        if self.completeness != "unknown" and not known:
            raise ValueError("部分/完整用量必须有观测")
        if self.completeness == "complete" and (
            self.input_tokens is None or self.output_tokens is None
        ):
            raise ValueError("完整用量必须具备输入和输出总量")
        partitions = (
            self.uncached_input_tokens,
            self.cache_read_input_tokens,
            self.cache_creation_input_tokens,
        )
        if self.input_tokens is not None:
            subtotal = sum(value for value in partitions if value is not None)
            if subtotal > self.input_tokens:
                raise ValueError("输入子集超过输入总量")
            if all(value is not None for value in partitions) and subtotal != self.input_tokens:
                raise ValueError("输入分项之和与总量不一致")
        if (
            self.output_tokens is not None
            and self.reasoning_output_tokens is not None
            and self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("推理用量超过输出总量")
        return self

    def validate_successor(self, previous: UsageObservation) -> None:
        """完整快照不能丢字段或回退；终值允许补明细，不允许修改已知数值。"""
        if previous.completeness == "complete" and self.completeness != "complete":
            raise ValueError("完整用量不可降级")
        for field in TOKEN_FIELDS:
            before, after = getattr(previous, field), getattr(self, field)
            if before is not None and (
                after is None
                or after < before
                or (previous.completeness == "complete" and after != before)
            ):
                raise ValueError("累计用量回退或修改了已知终值")


class ModelAttemptStarted(ContractModel):
    type: Literal["model_attempt_started"] = "model_attempt_started"
    attempt_id: UUID
    step: int = Field(ge=1, strict=True)
    index: int = Field(ge=1, le=32, strict=True)
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    requested_model: ModelIdentifier


class ModelUsageObserved(ContractModel):
    type: Literal["model_usage_observed"] = "model_usage_observed"
    attempt_id: UUID
    usage: UsageObservation
    actual_model: ModelIdentifier | None = None
    response_id: ModelIdentifier | None = None
    billing: ResponseBillingMetadata | None = None

    @model_validator(mode="after")
    def validate_billing(self) -> Self:
        if self.billing is not None:
            self.billing.validate_usage(self.usage)
        return self


class ModelAttemptFinished(ContractModel):
    type: Literal["model_attempt_finished"] = "model_attempt_finished"
    attempt_id: UUID
    outcome: Literal["completed", "failed", "cancelled", "interrupted"]
    error: AgentFailure | None = None

    @model_validator(mode="after")
    def validate_error(self) -> Self:
        if (self.outcome == "completed") != (self.error is None):
            raise ValueError("失败尝试需要错误，成功尝试不能携带错误")
        return self


class ModelAttempt(ContractModel):
    attempt_id: UUID
    step: int = Field(ge=1)
    index: int = Field(ge=1, le=32)
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    requested_model: ModelIdentifier
    actual_model: ModelIdentifier | None = None
    response_id: ModelIdentifier | None = None
    usage: UsageObservation = Field(default_factory=UsageObservation)
    billing: ResponseBillingMetadata = Field(default_factory=ResponseBillingMetadata)
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"] = "running"
    error: AgentFailure | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        self.billing.validate_usage(self.usage)
        if self.status == "running":
            if self.error is not None or self.finished_at is not None:
                raise ValueError("运行中尝试不能预置终态")
        else:
            if self.finished_at is None or (self.status == "completed") != (self.error is None):
                raise ValueError("模型尝试终态不完整")
            if self.status == "completed" and (
                self.usage.completeness != "complete"
                or self.actual_model is None
                or self.response_id is None
            ):
                raise ValueError("成功尝试缺少完整用量或响应身份")
        return self
