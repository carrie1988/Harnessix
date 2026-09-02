from __future__ import annotations

from dataclasses import dataclass

from anthropic.types import (
    InputJSONDelta,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    RawMessageStreamEvent,
    TextBlock,
    ToolUseBlock,
)
from anthropic.types import (
    TextDelta as AnthropicTextDelta,
)
from pydantic import TypeAdapter

from harnessix.agent.models import Usage
from harnessix.models._bounded_http import InvalidWireData
from harnessix.models._json import strict_json
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)

_EVENT: TypeAdapter[RawMessageStreamEvent] = TypeAdapter(RawMessageStreamEvent)
_COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def validate_event(value: object) -> RawMessageStreamEvent:
    return _EVENT.validate_python(value, strict=True)


@dataclass
class Block:
    type: str
    text: str = ""
    call_id: str = ""
    tool: str = ""
    arguments: str = ""
    closed: bool = False


class AnthropicStream:
    def __init__(self, request: ModelRequest, names: dict[str, str], *, parallel: bool) -> None:
        self._request = request
        self._names = names
        self._parallel = parallel
        self._started = False
        self._stopped = False
        self._delta_seen = False
        self._finish: str | None = None
        self._blocks: dict[int, Block] = {}
        self._counts: dict[str, int] = {}
        self._characters = 0
        self._call_ids: set[str] = set()

    def feed(self, value: RawMessageStreamEvent) -> list[ProviderEvent]:
        event = validate_event(value.model_dump(warnings="error"))
        if self._stopped:
            raise InvalidWireData("消息终态之后出现额外事件")
        if isinstance(event, RawMessageStartEvent):
            message = event.message
            if (
                self._started
                or message.role != "assistant"
                or message.content
                or message.stop_reason is not None
            ):
                raise InvalidWireData("消息起始状态无效")
            self._started = True
            self._update_counts(message.usage.model_dump())
            return [ResponseStarted(response_id=message.id)]
        if not self._started:
            raise InvalidWireData("消息尚未开始")
        if isinstance(event, RawContentBlockStartEvent):
            if self._delta_seen or event.index != len(self._blocks) or len(self._blocks) >= 128:
                raise InvalidWireData("Block 起始 index 或顺序无效")
            content = event.content_block
            if isinstance(content, TextBlock):
                if content.citations:
                    raise InvalidWireData("未开启 Citations")
                self._blocks[event.index] = Block(type="text", text=content.text)
                self._add_characters(len(content.text))
                events: list[ProviderEvent] = [TextStarted(content_id=str(event.index))]
                if content.text:
                    events.append(TextDelta(content_id=str(event.index), delta=content.text))
                return events
            if isinstance(content, ToolUseBlock):
                if (
                    not content.id
                    or len(content.id) > 256
                    or content.id in self._call_ids
                    or content.name not in self._names
                    or content.input
                    or content.toolset_name is not None
                    or (content.caller is not None and content.caller.type != "direct")
                ):
                    raise InvalidWireData("工具 Block 身份或能力无效")
                self._call_ids.add(content.id)
                if len(self._call_ids) > self._request.budget.max_tool_calls_per_step or (
                    not self._parallel and len(self._call_ids) > 1
                ):
                    raise InvalidWireData("工具数量超过能力或预算")
                self._blocks[event.index] = Block(
                    type="tool_use", call_id=content.id, tool=self._names[content.name]
                )
                return []
            raise InvalidWireData("不支持的内容 Block；不丢弃私有签名或推理后继续")
        if isinstance(event, RawContentBlockDeltaEvent | RawContentBlockStopEvent):
            block = self._blocks.get(event.index)
            if self._delta_seen or block is None or block.closed:
                raise InvalidWireData("Block 未开始、已关闭或顺序无效")
            if isinstance(event, RawContentBlockStopEvent):
                block.closed = True
                return []
            if isinstance(event.delta, AnthropicTextDelta) and block.type == "text":
                self._add_characters(len(event.delta.text))
                block.text += event.delta.text
                return [TextDelta(content_id=str(event.index), delta=event.delta.text)]
            if isinstance(event.delta, InputJSONDelta) and block.type == "tool_use":
                self._add_characters(len(event.delta.partial_json))
                block.arguments += event.delta.partial_json
                return []
            raise InvalidWireData("Delta 与内容 Block 不匹配")
        if isinstance(event, RawMessageDeltaEvent):
            if any(not block.closed for block in self._blocks.values()):
                raise InvalidWireData("消息 Delta 到达时 Block 尚未关闭")
            if event.delta.container is not None:
                raise InvalidWireData("未开启服务器工具容器")
            if event.delta.stop_reason is not None:
                if self._finish is not None:
                    raise InvalidWireData("重复结束原因")
                self._finish = event.delta.stop_reason
            self._delta_seen = True
            self._update_counts(event.usage.model_dump())
            return []
        if isinstance(event, RawMessageStopEvent):
            if not self._delta_seen or self._finish is None:
                raise InvalidWireData("消息结束缺少终态原因或最终 Usage")
            self._stopped = True
            return []
        raise InvalidWireData("不支持的流事件")

    def _update_counts(self, values: dict[str, object]) -> None:
        for key in _COUNTS:
            value = values.get(key)
            if value is None:
                continue
            if type(value) is not int or value < self._counts.get(key, 0):
                raise InvalidWireData("Usage 非整数、负数或累计回退")
            self._counts[key] = value
        server = values.get("server_tool_use")
        if isinstance(server, dict) and any(server.values()):
            raise InvalidWireData("未开启服务器工具计费")

    def _add_characters(self, count: int) -> None:
        self._characters += count
        if self._characters > self._request.budget.max_output_chars:
            raise InvalidWireData("文本或工具参数超过大小预算")

    def finish(self) -> list[ProviderEvent]:
        if not self._stopped or any(key not in self._counts for key in _COUNTS):
            raise InvalidWireData("缺少 message_stop 或无法确定完整 Usage")
        usage = Usage(
            input_tokens=sum(self._counts[key] for key in _COUNTS if key != "output_tokens"),
            output_tokens=self._counts["output_tokens"],
        )
        if self._finish == "model_context_window_exceeded":
            return [ResponseFailed(code="context_overflow")]
        reasons = {
            "end_turn": ResponseCompleted(usage=usage),
            "stop_sequence": ResponseCompleted(usage=usage),
            "tool_use": ResponseCompleted(finish_reason="tool_calls", usage=usage),
            "max_tokens": ResponseCompleted(finish_reason="max_output_tokens", usage=usage),
            "refusal": ResponseCompleted(finish_reason="content_filter", usage=usage),
            "pause_turn": ResponseCompleted(finish_reason="unknown", usage=usage),
        }
        if self._finish not in reasons:
            raise InvalidWireData("未知 Stop Reason")
        if self._finish in {"end_turn", "stop_sequence", "tool_use"}:
            if bool(self._call_ids) != (self._finish == "tool_use"):
                raise InvalidWireData("结束原因与工具调用不匹配")
            if not self._call_ids and not any(block.text for block in self._blocks.values()):
                raise InvalidWireData("响应没有语义内容")
        events: list[ProviderEvent] = []
        for index, block in self._blocks.items():
            if block.type == "text":
                events.append(TextCompleted(content_id=str(index), text=block.text))
            elif self._finish == "tool_use":
                # 无 Delta 的空输入使用起始块明确提供的 {}，不是修复截断 JSON。
                arguments = strict_json(block.arguments) if block.arguments else {}
                if not isinstance(arguments, dict):
                    raise InvalidWireData("工具参数必须是 JSON object")
                events.append(
                    ToolCallCompleted(call_id=block.call_id, tool=block.tool, arguments=arguments)
                )
        return [*events, reasons[self._finish]]
