"""Headless Agent Protocol服务：以有界stdio帧运行Headless App Server。"""

from __future__ import annotations

import asyncio
from typing import BinaryIO

from harnessix.app_server.server import AgentProtocolServer, ConnectionState


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
    """运行可多路复用的单客户端stdio JSONL；EOF后关闭并收敛未决请求。"""

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
    stopping = asyncio.Event()
    slots = asyncio.Semaphore(server.limits.max_pending_requests)
    pending: set[asyncio.Task[None]] = set()

    async def enqueue(frame: bytes) -> None:
        while True:
            outbox_drained.clear()
            try:
                outbox.put_nowait(frame)
                return
            except asyncio.QueueFull:
                await outbox_drained.wait()

    async def dispatch(frame: bytes) -> None:
        try:
            responses = await server.process_frame(frame)
            for response in responses:
                try:
                    async with asyncio.timeout(outbound_timeout_seconds):
                        await enqueue(response)
                except TimeoutError:
                    stopping.set()
                    return
        finally:
            slots.release()

    def settled(task: asyncio.Task[None]) -> None:
        pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            stopping.set()

    try:
        while True:
            if writer_task.done() or stopping.is_set():
                break
            line = await asyncio.to_thread(
                input_stream.readline, server.limits.max_message_bytes + 1
            )
            if not line:
                break
            if server.state is not ConnectionState.READY:
                responses = await server.process_frame(line)
                for response in responses:
                    try:
                        async with asyncio.timeout(outbound_timeout_seconds):
                            await enqueue(response)
                    except TimeoutError:
                        stopping.set()
                        break
                continue
            await slots.acquire()
            task = asyncio.create_task(dispatch(line), name="harnessix-stdio-request")
            pending.add(task)
            task.add_done_callback(settled)
    finally:
        await server.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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
