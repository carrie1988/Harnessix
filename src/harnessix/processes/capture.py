"""独立等待退出/管道终止；回调只保留有界前缀，不建立无界消息队列。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable

from harnessix.processes.contracts import ProcessLimits, ProcessStream, StopReason


class _Stream:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.observed = 0
        self.digest = hashlib.sha256()
        self.eof = False
        self.forced = False

    def feed(self, data: bytes) -> None:
        if self.forced:
            return
        self.observed += len(data)
        self.digest.update(data)
        self.buffer.extend(data[: max(0, self.limit - len(self.buffer))])

    def result(self) -> ProcessStream:
        return ProcessStream(
            data_base64=base64.b64encode(self.buffer).decode("ascii"),
            captured_bytes=len(self.buffer),
            observed_bytes=self.observed,
            observed_sha256=self.digest.hexdigest(),
            truncated=self.observed > len(self.buffer),
            eof=self.eof,
        )


class CaptureProtocol(asyncio.SubprocessProtocol):
    def __init__(self, limits: ProcessLimits, stop: Callable[[StopReason], None]) -> None:
        loop = asyncio.get_running_loop()
        self.exited: asyncio.Future[None] = loop.create_future()
        self.closed: asyncio.Future[None] = loop.create_future()
        self.streams = {1: _Stream(limits.stdout_bytes), 2: _Stream(limits.stderr_bytes)}
        self.transport: asyncio.SubprocessTransport | None = None
        self.threshold = limits.stop_output_bytes
        self.stop = stop

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.SubprocessTransport)
        self.transport = transport

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        self.streams[fd].feed(data)
        if sum(stream.observed for stream in self.streams.values()) >= self.threshold:
            self.stop("output_limit")
            self.close_pipes()

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        stream = self.streams[fd]
        stream.eof = exc is None and not stream.forced
        if exc is not None:
            self.stop("io_error")

    def process_exited(self) -> None:
        if not self.exited.done():
            self.exited.set_result(None)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            self.stop("io_error")
        if not self.closed.done():
            self.closed.set_result(None)

    def close_pipes(self) -> None:
        if self.transport is None:
            return
        for fd, stream in self.streams.items():
            if not stream.eof:
                stream.forced = True
                pipe = self.transport.get_pipe_transport(fd)
                if pipe is not None:
                    pipe.close()
