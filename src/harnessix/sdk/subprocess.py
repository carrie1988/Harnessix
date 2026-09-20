"""Agent SDK子进程传输：有界归并JSON-RPC Response并收敛进程生命周期。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from harnessix.protocol.contracts import ProtocolLimits
from harnessix.sdk.errors import AgentSDKError
from harnessix.sdk.response import _decode_response


def _frame_id(frame: bytes) -> str | int:
    try:
        wire = json.loads(frame)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AgentSDKError("invalid_request", "Request不是有效JSON") from None
    if not isinstance(wire, dict):
        raise AgentSDKError("invalid_request", "Request必须是JSON对象")
    request_id = wire.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise AgentSDKError("invalid_request", "Request缺少有效JSON-RPC id")
    return request_id


@dataclass(frozen=True, slots=True)
class SubprocessTransportSnapshot:
    """不含请求身份和stderr正文的子进程传输资源快照。"""

    state: str
    max_pending_requests: int
    pending_requests: int
    abandoned_requests: int
    stderr_tail_bytes: int
    failure_code: str | None


class _RequestCapacity:
    """让活动与已取消待迟到Response的请求共享同一容量。"""

    def __init__(self, limit: int) -> None:
        self._slots = asyncio.BoundedSemaphore(limit)
        self._unavailable = asyncio.Event()

    def mark_unavailable(self) -> None:
        self._unavailable.set()

    def release(self, count: int = 1) -> None:
        for _ in range(count):
            self._slots.release()

    async def reserve(self, check_available: Callable[[], None]) -> None:
        acquired = asyncio.create_task(self._slots.acquire(), name="harnessix-sdk-request-slot")
        unavailable = asyncio.create_task(
            self._unavailable.wait(), name="harnessix-sdk-unavailable"
        )
        transferred = False
        try:
            await asyncio.wait((acquired, unavailable), return_when=asyncio.FIRST_COMPLETED)
            check_available()
            await acquired
            transferred = True
        finally:
            if not acquired.done():
                acquired.cancel()
            if not unavailable.done():
                unavailable.cancel()
            await asyncio.gather(acquired, unavailable, return_exceptions=True)
            if (
                not transferred
                and acquired.done()
                and not acquired.cancelled()
                and acquired.exception() is None
                and acquired.result()
            ):
                self._slots.release()


class _ResponseRouter:
    """拥有未决Future和迟到Response身份，不拥有子进程。"""

    def __init__(self, max_message_bytes: int, max_pending_requests: int) -> None:
        self.max_message_bytes = max_message_bytes
        self.pending: dict[str | int, asyncio.Future[bytes]] = {}
        self.abandoned: set[str | int] = set()
        self.failure: tuple[str, str] | None = None
        self._capacity = _RequestCapacity(max_pending_requests)

    def fail(self, code: str, message: str) -> None:
        if self.failure is None:
            self.failure = (code, message)
        self._capacity.mark_unavailable()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(AgentSDKError(code, message))
        self._capacity.release(len(self.pending) + len(self.abandoned))
        self.pending.clear()
        self.abandoned.clear()

    def raise_if_failed(self) -> None:
        if self.failure is not None:
            raise AgentSDKError(*self.failure)

    async def reserve(self) -> None:
        await self._capacity.reserve(self.raise_if_failed)

    def register(self, request_id: str | int) -> asyncio.Future[bytes]:
        if request_id in self.pending or request_id in self.abandoned:
            self._capacity.release()
            raise AgentSDKError("duplicate_request_id", "存在相同id的未决Request")
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        return future

    def abandon(self, request_id: str | int, future: asyncio.Future[bytes]) -> None:
        if self.pending.pop(request_id, None) is not None:
            future.cancel()
            self.abandoned.add(request_id)

    def discard(self, request_id: str | int, future: asyncio.Future[bytes]) -> None:
        if self.pending.pop(request_id, None) is not None:
            future.cancel()
            self._capacity.release()

    async def read(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while response := await process.stdout.readline():
                decoded = _decode_response(response, max_message_bytes=self.max_message_bytes)
                response_id = decoded.id
                if response_id is None:
                    self.fail("invalid_response", "App Server Response缺少有效id")
                    return
                if response_id in self.abandoned:
                    self.abandoned.remove(response_id)
                    self._capacity.release()
                    continue
                future = self.pending.pop(response_id, None)
                if future is None:
                    self.fail("invalid_response", "App Server返回了未知Response id")
                    return
                self._capacity.release()
                if not future.done():
                    future.set_result(response)
        except asyncio.CancelledError:
            self.fail("server_closed", "App Server响应读取已经停止")
            raise
        except AgentSDKError as error:
            self.fail(error.code, error.message)
        except ValueError:
            self.fail("invalid_response", "App Server Response超过字节上限")
        except (OSError, RuntimeError):
            self.fail("server_closed", "App Server响应读取失败")
        else:
            self.fail("server_closed", "App Server输出流已经关闭")


class _ChildProcess:
    """拥有子进程、标准流锁和Reader/Stderr后台任务。"""

    def __init__(
        self, command: tuple[str, ...], max_message_bytes: int, router: _ResponseRouter
    ) -> None:
        self.command = command
        self.max_message_bytes = max_message_bytes
        self.router = router
        self.process: asyncio.subprocess.Process | None = None
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.stderr_tail = bytearray()

    async def start(self, closed: bool) -> asyncio.subprocess.Process:
        async with self.start_lock:
            if closed:
                raise AgentSDKError("server_closed", "App Server传输已经关闭")
            self.router.raise_if_failed()
            if self.process is None:
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        *self.command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=self.max_message_bytes + 1,
                    )
                except OSError:
                    raise AgentSDKError("server_start_failed", "App Server子进程启动失败") from None
                self.reader_task = asyncio.create_task(
                    self.router.read(self.process), name="harnessix-sdk-reader"
                )
                self.stderr_task = asyncio.create_task(
                    self._drain_stderr(self.process), name="harnessix-sdk-stderr"
                )
            return self.process

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            self.stderr_tail.extend(chunk)
            if len(self.stderr_tail) > 65_536:
                del self.stderr_tail[:-65_536]

    async def write(self, process: asyncio.subprocess.Process, frame: bytes) -> None:
        assert process.stdin is not None
        async with self.write_lock:
            self.router.raise_if_failed()
            if process.returncode is not None:
                raise AgentSDKError("server_closed", "App Server已经退出")
            try:
                process.stdin.write(frame)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise AgentSDKError("server_closed", "App Server输入流写入失败") from None

    async def shutdown(self, graceful_seconds: float, terminate_seconds: float) -> None:
        async with self.start_lock:
            process = self.process
        if process is None:
            return
        async with self.write_lock:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        await _wait_for_exit(process, graceful_seconds, terminate_seconds)
        if self.reader_task is not None:
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self.stderr_task is not None:
            await self.stderr_task


async def _wait_for_exit(
    process: asyncio.subprocess.Process,
    graceful_seconds: float,
    terminate_seconds: float,
) -> None:
    try:
        async with asyncio.timeout(graceful_seconds):
            await process.wait()
    except TimeoutError:
        if process.returncode is None:
            process.terminate()
        try:
            async with asyncio.timeout(terminate_seconds):
                await process.wait()
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()


class SubprocessAgentTransport:
    """支持有界并发请求的单进程stdio传输；Response按JSON-RPC id归并。"""

    def __init__(
        self,
        command: Sequence[str],
        *,
        max_message_bytes: int = 1_048_576,
        max_pending_requests: int = 64,
        graceful_shutdown_timeout_seconds: float = 10.0,
        terminate_timeout_seconds: float = 5.0,
    ) -> None:
        if not command:
            raise ValueError("App Server命令不能为空")
        try:
            limits = ProtocolLimits(
                max_message_bytes=max_message_bytes,
                max_pending_requests=max_pending_requests,
            )
        except ValidationError:
            raise ValueError("App Server传输限制无效") from None
        if graceful_shutdown_timeout_seconds <= 0 or terminate_timeout_seconds <= 0:
            raise ValueError("App Server关闭超时必须大于零")
        self.max_message_bytes = limits.max_message_bytes
        self.max_pending_requests = limits.max_pending_requests
        self.graceful_shutdown_timeout_seconds = graceful_shutdown_timeout_seconds
        self.terminate_timeout_seconds = terminate_timeout_seconds
        self._router = _ResponseRouter(self.max_message_bytes, self.max_pending_requests)
        self._child = _ChildProcess(tuple(command), self.max_message_bytes, self._router)
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def snapshot(self) -> SubprocessTransportSnapshot:
        if self._closed:
            state = "closed" if self._close_task and self._close_task.done() else "closing"
        elif self._router.failure is not None:
            state = "failed"
        elif self._child.process is None:
            state = "not_started"
        elif self._child.process.returncode is None:
            state = "running"
        else:
            state = "exited"
        return SubprocessTransportSnapshot(
            state=state,
            max_pending_requests=self.max_pending_requests,
            pending_requests=len(self._router.pending),
            abandoned_requests=len(self._router.abandoned),
            stderr_tail_bytes=len(self._child.stderr_tail),
            failure_code=None if self._router.failure is None else self._router.failure[0],
        )

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        request_id = _frame_id(frame)
        process = await self._child.start(self._closed)
        await self._router.reserve()
        future = self._router.register(request_id)
        try:
            if self._closed:
                raise AgentSDKError("server_closed", "App Server传输正在关闭")
            await self._child.write(process, frame)
            return (await asyncio.shield(future),)
        except asyncio.CancelledError:
            self._router.abandon(request_id, future)
            raise
        except BaseException:
            self._router.discard(request_id, future)
            raise

    async def notify(self, frame: bytes) -> None:
        process = await self._child.start(self._closed)
        if self._closed:
            raise AgentSDKError("server_closed", "App Server传输正在关闭")
        await self._child.write(process, frame)

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._closed = True
                self._router.fail("server_closed", "App Server传输已经关闭")
                self._close_task = asyncio.create_task(
                    self._child.shutdown(
                        self.graceful_shutdown_timeout_seconds,
                        self.terminate_timeout_seconds,
                    ),
                    name="harnessix-sdk-close",
                )
            close_task = self._close_task
        await asyncio.shield(close_task)
