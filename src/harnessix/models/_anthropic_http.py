from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2

from harnessix.models._anthropic_stream import validate_event
from harnessix.models._bounded_http import BoundedStream, InvalidWireData
from harnessix.models._json import strict_json
from harnessix.models.config import AnthropicConfig


class MessageFrames:
    """在 SDK 忽略未知 SSE 事件或补写 type 前检查原始帧。"""

    def __init__(self) -> None:
        self.stopped = False

    def validate(self, name: bytes, data: bytes) -> None:
        allowed = {
            b"message_start",
            b"message_delta",
            b"message_stop",
            b"content_block_start",
            b"content_block_delta",
            b"content_block_stop",
            b"ping",
            b"error",
        }
        if self.stopped or name not in allowed:
            raise InvalidWireData("不支持的 SSE 事件或终态后数据")
        value = strict_json(data)
        if not isinstance(value, dict) or value.get("type") != name.decode("ascii"):
            raise InvalidWireData("SSE 事件名称与 JSON type 不一致")
        if name not in {b"ping", b"error"}:
            # SDK 默认解析会把 bool Usage 转成 int；必须在信息丢失前严格验证。
            validate_event(value)
        if name == b"message_stop":
            self.stopped = True


class AnthropicByteStream(httpx2.AsyncByteStream):
    def __init__(self, source: httpx2.AsyncByteStream, config: AnthropicConfig) -> None:
        self._bounded = BoundedStream(source, config, validate_frame=MessageFrames().validate)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._bounded.__aiter__()

    async def aclose(self) -> None:
        await self._bounded.aclose()


class AnthropicTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, config: AnthropicConfig) -> None:
        self._inner = inner
        self._config = config

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise InvalidWireData("不接受压缩响应")
        if not isinstance(response.stream, httpx2.AsyncByteStream):
            await response.aclose()
            raise InvalidWireData("响应不是异步流")
        response.stream = AnthropicByteStream(response.stream, self._config)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
