"""Headless Agent Protocol服务：以有界stdio帧运行Headless App Server。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import BinaryIO

from harnessix.app_server.server import AgentProtocolServer, ConnectionState


class _StdioReader:
    """用守护线程读取同步stdin，避免Writer故障时遗留非守护线程。"""

    def __init__(self, stream: BinaryIO, max_message_bytes: int) -> None:
        self._stream = stream
        self._max_message_bytes = max_message_bytes
        self._loop = asyncio.get_running_loop()
        self._inbox: asyncio.Queue[bytes | BaseException] = asyncio.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._future_lock = threading.Lock()
        self._put_future: concurrent.futures.Future[None] | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="harnessix-stdio-reader",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                item: bytes | BaseException = self._stream.readline(self._max_message_bytes + 1)
            except BaseException as error:
                item = error
            if self._stopped.is_set():
                return
            try:
                future = asyncio.run_coroutine_threadsafe(self._inbox.put(item), self._loop)
            except RuntimeError:
                return
            with self._future_lock:
                self._put_future = future
            try:
                future.result()
            except (concurrent.futures.CancelledError, RuntimeError):
                return
            finally:
                with self._future_lock:
                    if self._put_future is future:
                        self._put_future = None
            if isinstance(item, BaseException) or not item:
                return

    async def readline(self) -> bytes:
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def stop(self) -> None:
        self._stopped.set()
        with self._future_lock:
            if self._put_future is not None:
                self._put_future.cancel()


class _StdioWriter:
    """单守护线程顺序写stdout；队列满或写阻塞时允许主协程有界退出。"""

    def __init__(self, stream: BinaryIO, max_outbound_messages: int) -> None:
        self._stream = stream
        self._loop = asyncio.get_running_loop()
        self._outbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_outbound_messages)
        self._space = asyncio.Event()
        self._done = asyncio.Event()
        self._stopped = threading.Event()
        self._future_lock = threading.Lock()
        self._take_future: concurrent.futures.Future[bytes | None] | None = None
        self.failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="harnessix-stdio-writer",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        failure: BaseException | None = None
        try:
            while not self._stopped.is_set():
                try:
                    future = asyncio.run_coroutine_threadsafe(self._outbox.get(), self._loop)
                except RuntimeError:
                    return
                with self._future_lock:
                    self._take_future = future
                try:
                    frame = future.result()
                except (concurrent.futures.CancelledError, RuntimeError):
                    return
                finally:
                    with self._future_lock:
                        if self._take_future is future:
                            self._take_future = None
                self._loop.call_soon_threadsafe(self._space.set)
                if frame is None:
                    break
                self._stream.write(frame)
                self._stream.flush()
        except BaseException as error:
            failure = error
        finally:
            try:
                self._loop.call_soon_threadsafe(self._finish, failure)
            except RuntimeError:
                pass

    def _finish(self, failure: BaseException | None) -> None:
        self.failure = failure
        self._space.set()
        self._done.set()

    async def enqueue(self, frame: bytes | None, *, limit: int, timeout_seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            if self._done.is_set():
                if self.failure is not None:
                    raise self.failure
                raise BrokenPipeError("stdio Writer已经关闭")
            self._space.clear()
            if self._outbox.qsize() < limit:
                try:
                    self._outbox.put_nowait(frame)
                except asyncio.QueueFull:
                    pass
                else:
                    return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("stdio出站队列等待超时")
            async with asyncio.timeout(remaining):
                await self._space.wait()

    async def close(self, *, limit: int, timeout_seconds: float) -> bool:
        if self._done.is_set():
            return True
        try:
            await self.enqueue(None, limit=limit, timeout_seconds=timeout_seconds)
            async with asyncio.timeout(timeout_seconds):
                await self._done.wait()
        except TimeoutError:
            self.stop()
            return False
        return True

    async def wait_done(self) -> None:
        await self._done.wait()

    def stop(self) -> None:
        self._stopped.set()
        with self._future_lock:
            if self._take_future is not None:
                self._take_future.cancel()


async def _read_until_stopping(
    reader: _StdioReader,
    writer: _StdioWriter,
    stopping: asyncio.Event,
) -> bytes | None:
    read_task = asyncio.create_task(reader.readline(), name="harnessix-stdio-read")
    writer_task = asyncio.create_task(writer.wait_done(), name="harnessix-stdio-writer-done")
    stop_task = asyncio.create_task(stopping.wait(), name="harnessix-stdio-stop")
    tasks = (read_task, writer_task, stop_task)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if writer_task in done or stop_task in done:
            return None
        return await read_task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_stdio(
    server: AgentProtocolServer,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    outbound_timeout_seconds: float = 5.0,
) -> None:
    """运行可多路复用的单客户端stdio JSONL；EOF后关闭并收敛未决请求。"""

    if outbound_timeout_seconds <= 0:
        raise ValueError("stdio出站超时必须大于零")
    reader = _StdioReader(input_stream, server.limits.max_message_bytes)
    writer = _StdioWriter(output_stream, server.limits.max_outbound_messages)
    stopping = asyncio.Event()
    slots: asyncio.Semaphore | None = None
    pending: set[asyncio.Task[None]] = set()
    outbound_timed_out = False

    async def enqueue(frame: bytes) -> None:
        await writer.enqueue(
            frame,
            limit=server.limits.max_outbound_messages,
            timeout_seconds=outbound_timeout_seconds,
        )

    async def dispatch(frame: bytes, limiter: asyncio.Semaphore) -> None:
        nonlocal outbound_timed_out
        try:
            responses = await server.process_frame(frame)
            for response in responses:
                try:
                    await enqueue(response)
                except TimeoutError:
                    outbound_timed_out = True
                    stopping.set()
                    return
        finally:
            limiter.release()

    def settled(task: asyncio.Task[None]) -> None:
        pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            stopping.set()

    try:
        while line := await _read_until_stopping(reader, writer, stopping):
            if server.state is not ConnectionState.READY:
                responses = await server.process_frame(line)
                for response in responses:
                    try:
                        await enqueue(response)
                    except TimeoutError:
                        outbound_timed_out = True
                        stopping.set()
                        break
                continue
            if slots is None:
                # READY后协商值冻结，避免在握手前捕获默认上限。
                slots = asyncio.Semaphore(server.limits.max_pending_requests)
            await slots.acquire()
            task = asyncio.create_task(dispatch(line, slots), name="harnessix-stdio-request")
            pending.add(task)
            task.add_done_callback(settled)
    finally:
        stopping.set()
        reader.stop()
        await server.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        closed = await writer.close(
            limit=server.limits.max_outbound_messages,
            timeout_seconds=outbound_timeout_seconds,
        )
        if not closed:
            outbound_timed_out = True
        writer.stop()
    if writer.failure is not None:
        raise writer.failure
    if outbound_timed_out:
        raise TimeoutError("stdio出站写入未在期限内完成")
