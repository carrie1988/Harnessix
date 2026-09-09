from __future__ import annotations

import asyncio
from typing import BinaryIO

from harnessix.app_server.server import AgentProtocolServer


async def _write(output: BinaryIO, frame: bytes) -> None:
    def write() -> None:
        output.write(frame)
        output.flush()

    await asyncio.to_thread(write)


async def run_stdio(
    server: AgentProtocolServer,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    outbound_timeout_seconds: float = 5.0,
) -> None:
    """运行单客户端stdio JSONL；EOF后等待已接受命令到持久边界。"""

    outbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=server.limits.max_outbound_messages)
    outbox_drained = asyncio.Event()
    writer_failure: BaseException | None = None

    async def writer() -> None:
        nonlocal writer_failure
        try:
            while True:
                frame = await outbox.get()
                outbox_drained.set()
                if frame is None:
                    break
                await _write(output_stream, frame)
        except BaseException as error:
            writer_failure = error
            raise

    writer_task = asyncio.create_task(writer(), name="harnessix-stdio-writer")
    overloaded = False

    async def enqueue(frame: bytes) -> None:
        while outbox.qsize() >= server.limits.max_outbound_messages:
            outbox_drained.clear()
            if outbox.qsize() >= server.limits.max_outbound_messages:
                await outbox_drained.wait()
        await outbox.put(frame)

    try:
        while True:
            if writer_task.done():
                break
            line = await asyncio.to_thread(
                input_stream.readline, server.limits.max_message_bytes + 1
            )
            if not line:
                break
            responses = await server.process_frame(line)
            for response in responses:
                try:
                    async with asyncio.timeout(outbound_timeout_seconds):
                        await enqueue(response)
                except TimeoutError:
                    overloaded = True
                    break
            if overloaded:
                break
    finally:
        await server.close()
        if not writer_task.done():
            try:
                async with asyncio.timeout(outbound_timeout_seconds):
                    await outbox.put(None)
                    await writer_task
            except TimeoutError:
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)
        else:
            await asyncio.gather(writer_task, return_exceptions=True)
    if writer_failure is not None and not isinstance(writer_failure, asyncio.CancelledError):
        raise writer_failure
