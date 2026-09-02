from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Protocol

import httpx

from harnessix.models.config import ModelHTTPConfig


class ByteStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


FrameValidator = Callable[[bytes, bytes], None]


class InvalidWireData(ValueError):
    """只携带固定诊断，不包含不可信服务端数据。"""


class BoundedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        source: ByteStream,
        config: ModelHTTPConfig,
        *,
        validate_frame: FrameValidator | None = None,
    ) -> None:
        self._source = source
        self._config = config
        self.seen_done = False
        self._closed = False
        self._validate_frame = validate_frame

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async with aclosing(self._iterate()) as iterator:
                async for chunk in iterator:
                    yield chunk
        finally:
            # SDK 在读取 HTTP 错误 body 时可能尚未返回 AsyncStream；这里也必须收口。
            await self.aclose()

    async def _iterate(self) -> AsyncGenerator[bytes, None]:
        total = chunks = frames = frame_size = 0
        line = bytearray()
        data_lines: list[bytes] = []
        event_name = b""
        previous_cr = False
        async for chunk in self._source:
            total += len(chunk)
            chunks += 1
            if total > self._config.max_response_bytes or chunks > self._config.max_chunks:
                raise InvalidWireData("响应超过传输上限")
            # 在 SDK SSE 解码前限制 frame；支持 LF、CRLF 和 CR，不缓存整个响应。
            for byte in chunk:
                if previous_cr and byte == 10:
                    previous_cr = False
                    continue
                previous_cr = byte == 13
                frame_size += 1
                if frame_size > self._config.max_frame_bytes:
                    raise InvalidWireData("SSE frame 超过上限")
                if byte not in (10, 13):
                    line.append(byte)
                    continue
                if not line:
                    frames += 1
                    if frames > self._config.max_chunks:
                        raise InvalidWireData("SSE frame 数超过上限")
                    data = b"\n".join(data_lines)
                    if self._validate_frame is not None and data:
                        self._validate_frame(event_name, data)
                    if data and self.seen_done:
                        raise InvalidWireData("传输终结符之后出现额外数据")
                    if data.startswith(b"[DONE]") and data != b"[DONE]":
                        raise InvalidWireData("传输终结符格式无效")
                    if data == b"[DONE]":
                        self.seen_done = True
                    data_lines.clear()
                    event_name = b""
                    frame_size = 0
                elif line.startswith(b"data:"):
                    value = bytes(line[5:])
                    data_lines.append(value[1:] if value.startswith(b" ") else value)
                elif line.startswith(b"event:"):
                    value = bytes(line[6:])
                    event_name = value[1:] if value.startswith(b" ") else value
                line.clear()
            yield chunk

    async def aclose(self) -> None:
        if not self._closed:
            await self._source.aclose()
            self._closed = True


class BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, config: ModelHTTPConfig) -> None:
        self._inner = inner
        self._config = config

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise InvalidWireData("不接受压缩响应")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise InvalidWireData("响应不是异步流")
        bounded = BoundedStream(response.stream, self._config)
        response.stream = bounded
        response.extensions["harnessix_stream"] = bounded
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
