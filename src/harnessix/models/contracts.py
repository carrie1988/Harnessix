from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Budget, Item, Usage
from harnessix.domain.models import ContractModel, ToolDescriptor


class ModelRequest(ContractModel):
    thread_id: UUID
    turn_id: UUID
    step: int = Field(ge=1)
    history: tuple[Item, ...]
    tools: tuple[ToolDescriptor, ...]
    budget: Budget


class ResponseStarted(ContractModel):
    type: Literal["response_started"] = "response_started"
    response_id: str = Field(min_length=1, max_length=256)


class TextStarted(ContractModel):
    type: Literal["text_started"] = "text_started"
    content_id: str = Field(min_length=1, max_length=256)


class TextDelta(ContractModel):
    type: Literal["text_delta"] = "text_delta"
    content_id: str = Field(min_length=1, max_length=256)
    delta: str = Field(max_length=1_000_000)


class TextCompleted(ContractModel):
    type: Literal["text_completed"] = "text_completed"
    content_id: str = Field(min_length=1, max_length=256)
    text: str = Field(max_length=1_000_000)


class ToolCallCompleted(ContractModel):
    type: Literal["tool_call_completed"] = "tool_call_completed"
    call_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(min_length=1, max_length=256)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ResponseCompleted(ContractModel):
    type: Literal["response_completed"] = "response_completed"
    finish_reason: Literal[
        "completed", "tool_calls", "max_output_tokens", "content_filter", "cancelled", "unknown"
    ] = "completed"
    usage: Usage = Field(default_factory=Usage)


class ResponseFailed(ContractModel):
    type: Literal["response_failed"] = "response_failed"
    code: Literal[
        "invalid_request",
        "authentication",
        "rate_limit",
        "quota",
        "content_policy",
        "provider_internal",
        "transport",
        "invalid_provider_output",
        "context_overflow",
        "cancelled",
        "unknown",
    ]
    retryable: bool = False


ProviderEvent = Annotated[
    ResponseStarted
    | TextStarted
    | TextDelta
    | TextCompleted
    | ToolCallCompleted
    | ResponseCompleted
    | ResponseFailed,
    Field(discriminator="type"),
]


class ModelProvider(Protocol):
    def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]: ...
