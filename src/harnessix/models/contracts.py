"""模型Provider适配：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Literal, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Budget, Item, Usage
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.domain.models import ContractModel, ToolDescriptor


class ModelRequest(ContractModel):
    thread_id: UUID
    turn_id: UUID
    step: int = Field(ge=1)
    history: tuple[Item, ...]
    tools: tuple[ToolDescriptor, ...]
    # 可选的稳定线上目录：整轮不变以保持前缀缓存；只有 tools 内的工具本步可执行。
    catalog_tools: tuple[ToolDescriptor, ...] | None = None
    instructions: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    budget: Budget
    consumed_usage: Usage | None = None
    input_tokens_upper_bound: int | None = Field(default=None, ge=1, strict=True)
    remaining_calls: int | None = Field(default=None, ge=1, le=1000, strict=True)
    # 按上一请求实际用量推算的可负担请求数，只是调度提示，不是准入或预付证明。
    affordable_requests: int | None = Field(default=None, ge=0, le=1000, strict=True)
    remaining_tokens: int | None = Field(default=None, ge=1)
    tool_choice: Literal["auto", "required"] = "auto"
    forced_tool: str | None = Field(default=None, min_length=1, max_length=256)
    # 工具视图给出的受限原因与解锁条件；只追加到本次目录说明，不改变可执行集合。
    tool_notice: str | None = Field(default=None, min_length=1, max_length=1024)

    @property
    def wire_tools(self) -> tuple[ToolDescriptor, ...]:
        return self.tools if self.catalog_tools is None else self.catalog_tools

    @model_validator(mode="after")
    def validate_tool_choice(self) -> Self:
        if self.catalog_tools is not None:
            catalog = {tool.name: tool for tool in self.catalog_tools}
            if len(catalog) != len(self.catalog_tools) or any(
                catalog.get(tool.name) != tool for tool in self.tools
            ):
                raise ValueError("可执行工具必须逐字属于稳定线上目录")
        names = {tool.name for tool in self.tools}
        if self.tool_choice == "required" and not names:
            raise ValueError("required工具选择至少需要一个可见工具")
        if self.forced_tool is not None and (
            self.tool_choice != "required" or self.forced_tool not in names
        ):
            raise ValueError("强制工具必须来自required可见目录")
        return self


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


class ToolCallRejected(ContractModel):
    """结构完整但不在本次目录的提案；无名称、参数或执行能力。"""

    type: Literal["tool_call_rejected"] = "tool_call_rejected"
    call_id: str = Field(min_length=1, max_length=256, strict=True)
    # invalid_arguments_json：开启参数修复后仍不是JSON对象的调用，同样无名称、参数或执行能力。
    reason: Literal["unregistered_tool", "invalid_arguments_json"] = "unregistered_tool"
    argument_chars: int = Field(ge=0, le=1_000_000, strict=True)


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
    | ToolCallRejected
    | ResponseCompleted
    | ResponseFailed
    | ModelAttemptStarted
    | ModelUsageObserved
    | ModelAttemptFinished,
    Field(discriminator="type"),
]


class ModelProvider(Protocol):
    def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]: ...


@runtime_checkable
class ModelInputTokenEstimator(Protocol):
    """可选纯能力：完整输入或已证明不变的前缀之外的输入上界，零 I/O。"""

    @property
    def input_token_identity(self) -> tuple[str, str]: ...

    def estimate_input_tokens(self, request: ModelRequest, *, prefix_items: int = 0) -> int: ...


@runtime_checkable
class RewrittenInputTokenEstimator(Protocol):
    """可选纯能力：上一请求视图被逐条改写后，本次超出上一请求已计入部分的输入上界，零 I/O。

    同位置出站消息与上一请求完全相同才视为已计入；被缩短或删除的内容不抵扣，
    其余消息与封装全额按 UTF-8 字节计入。调用方必须先以持久证据证明上一视图。
    """

    def estimate_rewritten_input_tokens(
        self, request: ModelRequest, previous_history: tuple[Item, ...]
    ) -> int: ...


@runtime_checkable
class CompactionSummaryProjection(Protocol):
    """可选纯承诺：Native 摘要投影以 JSON string 入 wire，占满预留的 ASCII 支配合法摘要。"""

    @property
    def compaction_summary_projection(self) -> Literal["native-document-json-string/v1"]: ...
