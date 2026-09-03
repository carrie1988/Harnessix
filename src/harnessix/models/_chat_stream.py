from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from openai.types.chat import ChatCompletionChunk
from pydantic import ValidationError

from harnessix.agent.billing import ResponseBillingMetadata
from harnessix.agent.models import Usage
from harnessix.agent.usage import ModelUsageObserved, UsageObservation
from harnessix.models._billing import merge_billing
from harnessix.models._bounded_http import InvalidWireData
from harnessix.models._json import strict_json
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)


@dataclass
class CallParts:
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    type: str | None = None


def validate_frame(name: bytes, data: bytes) -> None:
    if data == b"[DONE]":
        return
    if name not in {b"", b"message", b"error"}:
        raise InvalidWireData("不支持的 Chat SSE 事件")
    value = strict_json(data)
    if isinstance(value, dict) and value.get("error"):
        return  # 错误类型交给 SDK 分类，不保存原始错误内容。
    try:
        ChatCompletionChunk.model_validate(value, strict=True)
    except ValidationError:
        raise InvalidWireData("Chat SSE 结构或计数类型无效") from None


class ChatStream:
    def __init__(
        self, request: ModelRequest, names: dict[str, str], *, parallel: bool, attempt_id: UUID
    ) -> None:
        self._request = request
        self._names = names
        self._parallel = parallel
        self._response_id: str | None = None
        self._model: str | None = None
        self._attempt_id = attempt_id
        self._text = ""
        self._text_started = False
        self._characters = 0
        self._finish: str | None = None
        self._usage: Usage | None = None
        self._calls: dict[int, CallParts] = {}
        self._billing = ResponseBillingMetadata()
        self._observation = UsageObservation()
        self._last_observed: tuple[UsageObservation, ResponseBillingMetadata] | None = None

    def feed(self, value: ChatCompletionChunk) -> list[ProviderEvent]:
        chunk = ChatCompletionChunk.model_validate(value.model_dump(warnings="error"), strict=True)
        events: list[ProviderEvent] = []
        if self._response_id is None:
            self._response_id = chunk.id
            self._model = chunk.model
            events.append(ResponseStarted(response_id=chunk.id))
        elif chunk.id != self._response_id or chunk.model != self._model:
            raise InvalidWireData("响应 ID 或实际模型不一致")
        if self._usage is not None:
            raise InvalidWireData("Usage 之后出现额外 chunk")
        self._billing = merge_billing(
            self._billing,
            service_tier=None if chunk.service_tier == "auto" else chunk.service_tier,
        )
        if chunk.usage is not None:
            usage = chunk.usage
            if (
                self._finish is None
                or chunk.choices
                or usage.prompt_tokens < 0
                or usage.completion_tokens < 0
                or usage.total_tokens != usage.prompt_tokens + usage.completion_tokens
            ):
                raise InvalidWireData("Usage 结构或顺序无效")
            inputs, outputs = usage.prompt_tokens_details, usage.completion_tokens_details
            cached = inputs.cached_tokens if inputs else None
            written = inputs.cache_write_tokens if inputs else None
            observation = UsageObservation(
                completeness="complete",
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cache_read_input_tokens=cached,
                cache_creation_input_tokens=written,
                uncached_input_tokens=usage.prompt_tokens - cached - written
                if cached is not None and written is not None
                else None,
                reasoning_output_tokens=outputs.reasoning_tokens if outputs else None,
            )
            self._usage = Usage(
                input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens
            )
            self._observation = observation
            return [*events, *self._observe()]
        events.extend(self._observe())
        if self._finish is not None or len(chunk.choices) != 1 or chunk.choices[0].index != 0:
            raise InvalidWireData("choices 或结束顺序无效")
        choice = chunk.choices[0]
        delta = choice.delta
        if delta.role not in (None, "assistant") or delta.function_call is not None:
            raise InvalidWireData("不支持的消息类型")
        if delta.refusal:
            raise ContentRefused
        if delta.content:
            self._characters += len(delta.content)
            self._text += delta.content
            if not self._text_started:
                self._text_started = True
                events.append(TextStarted(content_id="text"))
            events.append(TextDelta(content_id="text", delta=delta.content))
        for part in delta.tool_calls or []:
            if not 0 <= part.index < self._request.budget.max_tool_calls_per_step:
                raise InvalidWireData("工具 index 超过上限")
            call = self._calls.setdefault(part.index, CallParts())
            if len(self._calls) > 1 and not self._parallel:
                raise InvalidWireData("Provider 不支持并行工具")
            if part.id is not None:
                if call.call_id is not None and call.call_id != part.id:
                    raise InvalidWireData("工具调用 ID 漂移")
                call.call_id = part.id
            if part.type is not None:
                if part.type != "function":
                    raise InvalidWireData("未知工具类型")
                call.type = part.type
            if part.function is not None:
                if part.function.name is not None:
                    if call.name is not None and call.name != part.function.name:
                        raise InvalidWireData("工具名称漂移")
                    call.name = part.function.name
                if part.function.arguments is not None:
                    self._characters += len(part.function.arguments)
                    call.arguments += part.function.arguments
        if self._characters > self._request.budget.max_output_chars:
            raise InvalidWireData("模型文本或参数超过大小上限")
        if choice.finish_reason is not None:
            self._finish = choice.finish_reason
        return events

    def _observe(self) -> list[ProviderEvent]:
        snapshot = (self._observation, self._billing)
        if snapshot == self._last_observed:
            return []
        event = ModelUsageObserved(
            attempt_id=self._attempt_id,
            response_id=self._response_id,
            actual_model=self._model,
            usage=self._observation,
            billing=self._billing if self._billing.observed else None,
        )
        self._last_observed = snapshot
        return [event]

    def finish(self, *, seen_done: bool) -> list[ProviderEvent]:
        if not seen_done or self._finish is None or self._usage is None:
            raise InvalidWireData("流缺少结束原因、Usage 或传输终结符")
        events: list[ProviderEvent] = []
        if self._text_started:
            events.append(TextCompleted(content_id="text", text=self._text))
        reasons: dict[str, ResponseCompleted] = {
            "stop": ResponseCompleted(usage=self._usage),
            "tool_calls": ResponseCompleted(finish_reason="tool_calls", usage=self._usage),
            "length": ResponseCompleted(finish_reason="max_output_tokens", usage=self._usage),
            "content_filter": ResponseCompleted(finish_reason="content_filter", usage=self._usage),
        }
        if self._finish not in reasons:
            raise InvalidWireData("不支持的结束原因")
        if self._finish in {"stop", "tool_calls"}:
            if bool(self._calls) != (self._finish == "tool_calls"):
                raise InvalidWireData("结束原因与工具调用不一致")
            if not self._calls and not self._text:
                raise InvalidWireData("模型响应没有语义内容")
            if sorted(self._calls) != list(range(len(self._calls))):
                raise InvalidWireData("工具 index 不连续")
            ids: set[str] = set()
            for index in sorted(self._calls):
                call = self._calls[index]
                if (
                    not call.call_id
                    or call.call_id in ids
                    or call.name not in self._names
                    or call.type != "function"
                ):
                    raise InvalidWireData("工具身份或名称无效")
                ids.add(call.call_id)
                arguments = strict_json(call.arguments)
                if not isinstance(arguments, dict):
                    raise InvalidWireData("工具参数必须为 JSON object")
                events.append(
                    ToolCallCompleted(
                        call_id=call.call_id,
                        tool=self._names[call.name],
                        arguments=arguments,
                    )
                )
        events.append(reasons[self._finish])
        return events


class ContentRefused(Exception):
    pass
