from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from harnessix.agent.errors import AgentFailure as AgentFailure
from harnessix.agent.ids import new_id
from harnessix.domain.models import (
    ApprovalRecord,
    ContractModel,
    EffectClass,
    TraceContext,
    utc_now,
)


class Budget(ContractModel):
    max_steps: int = Field(default=16, ge=1, le=1000)
    max_tokens: int = Field(default=100_000, ge=1)
    timeout_seconds: float = Field(default=120, gt=0, le=86400, allow_inf_nan=False)
    max_output_chars: int = Field(default=65536, ge=1, le=1_000_000)
    max_tool_calls_per_step: int = Field(default=32, ge=1, le=128)


class Usage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TurnStatus(StrEnum):
    ACCEPTED = "accepted"
    PREPARING_CONTEXT = "preparing_context"
    CALLING_MODEL = "calling_model"
    EXECUTING_TOOLS = "executing_tools"
    WAITING_APPROVAL = "waiting_approval"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_TURNS = frozenset(
    {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED}
)

TURN_TRANSITIONS = {
    TurnStatus.ACCEPTED: {TurnStatus.PREPARING_CONTEXT},
    TurnStatus.PREPARING_CONTEXT: {TurnStatus.CALLING_MODEL},
    TurnStatus.CALLING_MODEL: {TurnStatus.EXECUTING_TOOLS, TurnStatus.FINALIZING},
    TurnStatus.EXECUTING_TOOLS: {TurnStatus.PREPARING_CONTEXT, TurnStatus.WAITING_APPROVAL},
    TurnStatus.WAITING_APPROVAL: {TurnStatus.EXECUTING_TOOLS},
    TurnStatus.FINALIZING: {TurnStatus.COMPLETED},
    TurnStatus.CANCELLING: {TurnStatus.CANCELLED, TurnStatus.INTERRUPTED},
}


class ItemStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TextContent(ContractModel):
    kind: Literal["user_message", "assistant_message", "reasoning_summary"]
    text: str = Field(default="", max_length=1_000_000)


class ToolCallContent(ContractModel):
    kind: Literal["tool_call"] = "tool_call"
    call_id: UUID
    provider_call_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(min_length=1, max_length=256)
    tool_version: str = Field(min_length=1, max_length=128)
    effect_class: EffectClass
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    requires_approval: bool = False
    tool_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ToolResultContent(ContractModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: UUID
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"]
    output: JsonValue = None
    error: AgentFailure | None = None
    action_id: UUID | None = None


class ApprovalRequestContent(ContractModel):
    kind: Literal["approval_request"] = "approval_request"
    approval_id: UUID
    call_id: UUID
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ApprovalRecord | None = None
    policy_version: Literal["kernel-read-only/v1"] = "kernel-read-only/v1"


class PlanStep(ContractModel):
    step_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class PlanContent(ContractModel):
    kind: Literal["plan"] = "plan"
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=32)
    supersedes: UUID | None = None

    @model_validator(mode="after")
    def unique_steps(self) -> Self:
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("Plan 步骤 ID 必须唯一")
        if sum(step.status == "in_progress" for step in self.steps) > 1:
            raise ValueError("Plan 最多有一个进行中步骤")
        return self


class CompactionContent(ContractModel):
    kind: Literal["context_compaction"] = "context_compaction"
    source_item_ids: tuple[UUID, ...] = Field(min_length=1, max_length=4096)
    summary: str = Field(min_length=1, max_length=1_000_000)
    tokens_before: int = Field(ge=1)
    tokens_after: int = Field(ge=0)
    tokenizer: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_compression(self) -> Self:
        if len(set(self.source_item_ids)) != len(self.source_item_ids):
            raise ValueError("Compaction 来源 Item 不可重复")
        if self.tokens_after >= self.tokens_before:
            raise ValueError("Compaction 记录必须减少报告的 Token 数量")
        return self


class ErrorContent(ContractModel):
    kind: Literal["error"] = "error"
    failure: AgentFailure


ItemContent = Annotated[
    TextContent
    | ToolCallContent
    | ToolResultContent
    | ApprovalRequestContent
    | PlanContent
    | CompactionContent
    | ErrorContent,
    Field(discriminator="kind"),
]


class Item(ContractModel):
    item_id: UUID
    status: ItemStatus
    content: ItemContent
    error: AgentFailure | None = None


class Turn(ContractModel):
    turn_id: UUID
    request_id: str
    request_fingerprint: str
    status: TurnStatus = TurnStatus.ACCEPTED
    budget: Budget
    trace_context: TraceContext | None = None
    items: tuple[Item, ...] = ()
    model_steps: int = 0
    usage_step: int = 0
    usage: Usage = Field(default_factory=Usage)
    error: AgentFailure | None = None
    created_at: datetime
    completed_at: datetime | None = None


class Thread(ContractModel):
    thread_id: UUID
    workspace: str
    sequence: int = 0
    active_turn_id: UUID | None = None
    turns: tuple[Turn, ...] = ()
    created_at: datetime
    updated_at: datetime


class ThreadCreated(ContractModel):
    type: Literal["thread_created"] = "thread_created"
    workspace: str = Field(min_length=1, max_length=4096)

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Workspace 必须使用绝对路径")
        return value


class TurnStarted(ContractModel):
    type: Literal["turn_started"] = "turn_started"
    request_id: str = Field(min_length=1, max_length=256)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget: Budget
    trace_context: TraceContext | None = None


class TurnStateChanged(ContractModel):
    type: Literal["turn_state_changed"] = "turn_state_changed"
    status: TurnStatus
    error: AgentFailure | None = None


class ItemStarted(ContractModel):
    type: Literal["item_started"] = "item_started"
    item_id: UUID
    content: ItemContent


class ItemFinished(ContractModel):
    type: Literal["item_finished"] = "item_finished"
    item_id: UUID
    status: Literal[ItemStatus.COMPLETED, ItemStatus.FAILED, ItemStatus.CANCELLED]
    content: ItemContent
    error: AgentFailure | None = None


class UsageRecorded(ContractModel):
    type: Literal["usage_recorded"] = "usage_recorded"
    step: int = Field(ge=1)
    usage: Usage


EventPayload = Annotated[
    ThreadCreated | TurnStarted | TurnStateChanged | ItemStarted | ItemFinished | UsageRecorded,
    Field(discriminator="type"),
]


class EventDraft(ContractModel):
    schema_version: Literal[1, 2, 3] = 3
    event_id: UUID = Field(default_factory=new_id)
    turn_id: UUID | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    payload: EventPayload

    @model_serializer(mode="wrap")
    def serialize_event(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.schema_version < 3:
            payload = data.get("payload", {})
            for failure in (payload.get("error"), payload.get("content", {}).get("error")):
                if isinstance(failure, dict):
                    failure.pop("category", None)
        if self.schema_version == 1:
            content = data.get("payload", {}).get("content", {})
            if content.get("kind") == "tool_call":
                # 旧事件导出仍符合冻结的 v1 Schema，不泄漏兼容读取时补上的 v2 默认字段。
                content.pop("requires_approval", None)
                content.pop("tool_fingerprint", None)
        return data

    @model_validator(mode="after")
    def legacy_event_boundary(self) -> Self:
        if self.schema_version < 3 and isinstance(self.payload, ItemStarted | ItemFinished):
            if isinstance(self.payload.content, PlanContent | CompactionContent | ErrorContent):
                raise ValueError("Plan/Compaction/Error Item 需要 Agent Event v3")
        if self.schema_version == 1:
            payload = self.payload
            if (
                isinstance(payload, TurnStateChanged)
                and payload.status == TurnStatus.WAITING_APPROVAL
            ):
                raise ValueError("审批状态需要 Agent Event v2")
            if isinstance(payload, ItemStarted | ItemFinished):
                content = payload.content
                if isinstance(content, ApprovalRequestContent) or (
                    isinstance(content, ToolCallContent)
                    and (content.requires_approval or content.tool_fingerprint is not None)
                ):
                    raise ValueError("审批契约需要 Agent Event v2")
        return self


class AgentEvent(EventDraft):
    thread_id: UUID
    sequence: int = Field(ge=1)


class ItemDelta(ContractModel):
    thread_id: UUID
    turn_id: UUID
    item_id: UUID
    model_step: int = Field(ge=1)
    stream_sequence: int = Field(ge=1)
    delta: str
