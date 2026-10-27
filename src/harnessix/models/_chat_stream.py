"""模型Provider适配：增量解析Chat Completions流并验证事件顺序。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
from pydantic import JsonValue, ValidationError

from harnessix.agent.billing import ResponseBillingMetadata
from harnessix.agent.models import Usage
from harnessix.agent.usage import ModelUsageObserved, UsageObservation
from harnessix.models._billing import merge_billing
from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason
from harnessix.models._json import repaired_json_object, strict_json
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
    ToolCallRejected,
)


@dataclass
class CallParts:
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    type: str | None = None


def _complete_calls(
    calls: dict[int, CallParts], names: dict[str, str], *, repair_arguments: bool = False
) -> list[ToolCallCompleted | ToolCallRejected]:
    """先校验整组结构，再按本次精确目录分类；畸形组不释放任何提案。"""
    if sorted(calls) != list(range(len(calls))):
        raise ChatProtocolError(ChatProtocolReason.TOOL_INDEX_GAP)
    validated: list[tuple[str, str, dict[str, JsonValue] | None, int]] = []
    ids: set[str] = set()
    for index in sorted(calls):
        call = calls[index]
        if not call.call_id:
            raise ChatProtocolError(ChatProtocolReason.TOOL_ID_MISSING)
        if len(call.call_id) > 256:
            raise ChatProtocolError(ChatProtocolReason.TOOL_ID_LIMIT_EXCEEDED)
        if call.call_id in ids:
            raise ChatProtocolError(ChatProtocolReason.TOOL_ID_DUPLICATE)
        if not isinstance(call.name, str) or not 1 <= len(call.name) <= 256:
            raise ChatProtocolError(ChatProtocolReason.TOOL_NAME_UNKNOWN)
        if call.type != "function":
            raise ChatProtocolError(ChatProtocolReason.TOOL_TYPE_INVALID)
        ids.add(call.call_id)
        try:
            arguments: JsonValue = strict_json(call.arguments)
        except ValueError:
            if not repair_arguments:
                raise ChatProtocolError(ChatProtocolReason.TOOL_ARGUMENTS_INVALID) from None
            # 修复后仍非JSON对象：只拒绝本次调用（无效果），模型在原预算内重发，不使整Turn失败。
            arguments = repaired_json_object(call.arguments)
        if not isinstance(arguments, dict) and not repair_arguments:
            raise ChatProtocolError(ChatProtocolReason.TOOL_ARGUMENTS_NOT_OBJECT)
        validated.append(
            (
                call.call_id,
                call.name,
                arguments if isinstance(arguments, dict) else None,
                len(call.arguments),
            )
        )
    return [
        _proposal(call_id, name, arguments, chars, names)
        for call_id, name, arguments, chars in validated
    ]


def _proposal(
    call_id: str,
    name: str,
    arguments: dict[str, JsonValue] | None,
    argument_chars: int,
    names: dict[str, str],
) -> ToolCallCompleted | ToolCallRejected:
    if name not in names:
        return ToolCallRejected(call_id=call_id, argument_chars=argument_chars)
    if arguments is None:
        return ToolCallRejected(
            call_id=call_id, reason="invalid_arguments_json", argument_chars=argument_chars
        )
    return ToolCallCompleted(call_id=call_id, tool=names[name], arguments=arguments)


def validate_frame(name: bytes, data: bytes) -> None:
    if data == b"[DONE]":
        return
    if name not in {b"", b"message", b"error"}:
        raise ChatProtocolError(ChatProtocolReason.FRAME_EVENT_UNSUPPORTED)
    value = strict_json(data)
    if isinstance(value, dict) and value.get("error"):
        return  # 错误类型交给 SDK 分类，不保存原始错误内容。
    try:
        ChatCompletionChunk.model_validate(value, strict=True)
    except ValidationError:
        raise ChatProtocolError(ChatProtocolReason.FRAME_SCHEMA_INVALID) from None


def _complete_hit_miss_split(usage: CompletionUsage, cached: int | None) -> bool:
    """仅当命中与未命中均为非负整数、合计等于输入且命中与cached_tokens一致时成立。"""
    extra = usage.model_extra or {}
    hit, miss = extra.get("prompt_cache_hit_tokens"), extra.get("prompt_cache_miss_tokens")
    return (
        type(hit) is int
        and type(miss) is int
        and hit >= 0
        and miss >= 0
        and hit + miss == usage.prompt_tokens
        and hit == cached
    )


def _complete_usage_observation(
    usage: CompletionUsage, *, implicit_cache: bool = False
) -> UsageObservation:
    """使用原领域合同验证明细；原始计数和验证异常不进入诊断消息。"""
    inputs, outputs = usage.prompt_tokens_details, usage.completion_tokens_details
    cached = inputs.cached_tokens if inputs else None
    written = inputs.cache_write_tokens if inputs else None
    if inputs is not None and written is None:
        # DashScope兼容端点以cache_creation_input_tokens报告显式缓存创建；只接受非负整数。
        extra = (inputs.model_extra or {}).get("cache_creation_input_tokens")
        written = extra if type(extra) is int and extra >= 0 else None
    if written is None and _complete_hit_miss_split(usage, cached):
        # DeepSeek自动前缀缓存按命中/未命中完整拆分输入，无缓存创建计费项。
        written = 0
    if written is None and implicit_cache and type(cached) is int and cached >= 0:
        # 隐式缓存只报告命中数；创建按未缓存单价计，不存在单独的创建计费项。
        written = 0
    try:
        return UsageObservation(
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
    except ValidationError:
        raise ChatProtocolError(ChatProtocolReason.USAGE_DETAILS_INVALID) from None


class ChatStream:
    def __init__(
        self,
        request: ModelRequest,
        names: dict[str, str],
        *,
        parallel: bool,
        attempt_id: UUID,
        repair_arguments: bool = False,
        implicit_cache: bool = False,
    ) -> None:
        self._request = request
        self._repair_arguments = repair_arguments
        self._implicit_cache = implicit_cache
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
            raise ChatProtocolError(ChatProtocolReason.RESPONSE_IDENTITY_CHANGED)
        if self._usage is not None:
            raise ChatProtocolError(ChatProtocolReason.CHUNK_AFTER_USAGE)
        try:
            self._billing = merge_billing(
                self._billing,
                service_tier=None if chunk.service_tier == "auto" else chunk.service_tier,
            )
        except ValueError:
            raise ChatProtocolError(ChatProtocolReason.BILLING_METADATA_INVALID) from None
        if chunk.usage is not None and not chunk.choices:
            return [*events, *self._accept_usage(chunk.usage)]
        if chunk.usage is not None and self._finish is not None:
            raise ChatProtocolError(ChatProtocolReason.USAGE_SHAPE_OR_ORDER_INVALID)
        events.extend(self._observe())
        if self._finish is not None or len(chunk.choices) != 1 or chunk.choices[0].index != 0:
            raise ChatProtocolError(ChatProtocolReason.CHOICE_SHAPE_OR_ORDER_INVALID)
        choice = chunk.choices[0]
        delta = choice.delta
        if delta.role not in (None, "assistant") or delta.function_call is not None:
            raise ChatProtocolError(ChatProtocolReason.MESSAGE_TYPE_UNSUPPORTED)
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
                raise ChatProtocolError(ChatProtocolReason.TOOL_INDEX_LIMIT_EXCEEDED)
            call = self._calls.setdefault(part.index, CallParts())
            if len(self._calls) > 1 and not self._parallel:
                raise ChatProtocolError(ChatProtocolReason.PARALLEL_TOOL_CALLS_DISABLED)
            # 兼容服务可用空字符串表示本分片不再提供 ID；非空身份仍不可漂移。
            if part.id not in (None, ""):
                if call.call_id is not None and call.call_id != part.id:
                    raise ChatProtocolError(ChatProtocolReason.TOOL_ID_CHANGED)
                call.call_id = part.id
            if part.type is not None:
                if part.type != "function":
                    raise ChatProtocolError(ChatProtocolReason.TOOL_TYPE_INVALID)
                call.type = part.type
            if part.function is not None:
                # 空增量不覆盖名称；最终仍须有完整名称，非空名称仍不得漂移。
                if part.function.name:
                    if not 1 <= len(part.function.name) <= 256:
                        raise ChatProtocolError(ChatProtocolReason.TOOL_NAME_UNKNOWN)
                    if call.name is not None and call.name != part.function.name:
                        raise ChatProtocolError(ChatProtocolReason.TOOL_NAME_CHANGED)
                    call.name = part.function.name
                if part.function.arguments is not None:
                    self._characters += len(part.function.arguments)
                    call.arguments += part.function.arguments
        if self._characters > self._request.budget.max_output_chars:
            raise ChatProtocolError(ChatProtocolReason.OUTPUT_CHAR_LIMIT_EXCEEDED)
        if choice.finish_reason is not None:
            self._finish = choice.finish_reason
        if chunk.usage is not None:
            # 部分兼容端点（如DeepSeek）把用量放在携带finish_reason的末个分片中。
            if choice.finish_reason is None:
                raise ChatProtocolError(ChatProtocolReason.USAGE_SHAPE_OR_ORDER_INVALID)
            events.extend(self._accept_usage(chunk.usage))
        return events

    def _accept_usage(self, usage: CompletionUsage) -> list[ProviderEvent]:
        if (
            self._finish is None
            or usage.prompt_tokens < 0
            or usage.completion_tokens < 0
            or usage.total_tokens != usage.prompt_tokens + usage.completion_tokens
        ):
            raise ChatProtocolError(ChatProtocolReason.USAGE_SHAPE_OR_ORDER_INVALID)
        self._observation = _complete_usage_observation(usage, implicit_cache=self._implicit_cache)
        self._usage = Usage(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens)
        return self._observe()

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
            raise ChatProtocolError(ChatProtocolReason.COMPLETION_INCOMPLETE)
        events: list[ProviderEvent] = []
        if self._text_started:
            events.append(TextCompleted(content_id="text", text=self._text))
        finish = self._finish
        if finish == "stop" and self._calls and self._request.tool_choice == "required":
            # 部分OpenAI兼容端点在遵守required并返回原生调用后仍发送stop。
            finish = "tool_calls"
        reasons: dict[str, ResponseCompleted] = {
            "stop": ResponseCompleted(usage=self._usage),
            "tool_calls": ResponseCompleted(finish_reason="tool_calls", usage=self._usage),
            "length": ResponseCompleted(finish_reason="max_output_tokens", usage=self._usage),
            "content_filter": ResponseCompleted(finish_reason="content_filter", usage=self._usage),
        }
        if finish not in reasons:
            raise ChatProtocolError(ChatProtocolReason.FINISH_REASON_UNSUPPORTED)
        if finish in {"stop", "tool_calls"}:
            if bool(self._calls) != (finish == "tool_calls"):
                raise ChatProtocolError(ChatProtocolReason.FINISH_TOOL_MISMATCH)
            if not self._calls and not self._text:
                raise ChatProtocolError(ChatProtocolReason.SEMANTIC_OUTPUT_MISSING)
            events.extend(
                _complete_calls(self._calls, self._names, repair_arguments=self._repair_arguments)
            )
        events.append(reasons[finish])
        return events


class ContentRefused(Exception):
    pass
