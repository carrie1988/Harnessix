from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from harnessix.agent.ids import new_id
from harnessix.domain.models import ContractModel, EffectClass, TraceContext, utc_now


class AgentFailure(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(max_length=2000)
    retryable: bool = False


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
    TurnStatus.EXECUTING_TOOLS: {TurnStatus.PREPARING_CONTEXT},
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


class ToolResultContent(ContractModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: UUID
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"]
    output: JsonValue = None
    error: AgentFailure | None = None
    action_id: UUID | None = None


ItemContent = Annotated[
    TextContent | ToolCallContent | ToolResultContent, Field(discriminator="kind")
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
    schema_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=new_id)
    turn_id: UUID | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    payload: EventPayload


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
