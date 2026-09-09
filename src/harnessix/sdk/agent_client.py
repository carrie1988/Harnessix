from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Protocol, Self, cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from harnessix.protocol.contracts import (
    AGENT_PROTOCOL_VERSION,
    ApprovalRespondParams,
    ArtifactPageResult,
    ArtifactReadParams,
    ClientCapabilities,
    ClientInfo,
    EventsNextParams,
    EventsNextResult,
    EventsReplayParams,
    EventsReplayResult,
    InitializeParams,
    InitializeResult,
    JsonRpcNotification,
    JsonRpcRequest,
    ProtocolModel,
    PublicBudget,
    QuestionRespondParams,
    ThreadArchiveParams,
    ThreadCreateParams,
    ThreadForkParams,
    ThreadGetParams,
    ThreadListParams,
    ThreadListResult,
    ThreadResult,
    ThreadResumeParams,
    ThreadView,
    TurnCancelParams,
    TurnResult,
    TurnResumeParams,
    TurnRetryParams,
    TurnStartParams,
    TurnSteerParams,
    TurnView,
    validate_server_output,
)

if TYPE_CHECKING:
    from harnessix.app_server.server import AgentProtocolServer


class AgentSDKError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        path: tuple[str | int, ...] = (),
    ) -> None:
        location = "/".join(str(part) for part in path)
        super().__init__(f"{code}: {message}" + (f" [{location}]" if location else ""))
        self.code = code
        self.message = message
        self.retryable = retryable
        self.path = path


class AgentTransport(Protocol):
    async def exchange(self, frame: bytes) -> tuple[bytes, ...]: ...

    async def notify(self, frame: bytes) -> None: ...

    async def close(self) -> None: ...


class InProcessAgentTransport:
    """测试和嵌入模式复用完全相同的协议服务。"""

    def __init__(self, server: AgentProtocolServer) -> None:
        self.server = server

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        return await self.server.process_frame(frame)

    async def notify(self, frame: bytes) -> None:
        if await self.server.process_frame(frame):
            raise AgentSDKError("invalid_response", "Notification不得收到Response")

    async def close(self) -> None:
        await self.server.close()


class SubprocessAgentTransport:
    """支持并发请求的单进程stdio传输；Response按JSON-RPC id归并。"""

    def __init__(self, command: Sequence[str]) -> None:
        if not command:
            raise ValueError("App Server命令不能为空")
        self.command = tuple(command)
        self._process: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._pending: dict[str | int, asyncio.Future[bytes]] = {}
        self._abandoned: set[str | int] = set()
        self._reader_error: tuple[str, str] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False
        self.stderr_tail = bytearray()

    async def _start(self) -> asyncio.subprocess.Process:
        async with self._start_lock:
            if self._closed:
                raise AgentSDKError("server_closed", "App Server传输已经关闭")
            if self._reader_error is not None:
                raise AgentSDKError(*self._reader_error)
            if self._process is None:
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *self.command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError:
                    raise AgentSDKError("server_start_failed", "App Server子进程启动失败") from None
                self._reader_task = asyncio.create_task(
                    self._read_responses(self._process), name="harnessix-sdk-reader"
                )
                self._stderr_task = asyncio.create_task(
                    self._drain_stderr(self._process), name="harnessix-sdk-stderr"
                )
            return self._process

    @staticmethod
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

    def _fail_pending(self, code: str, message: str) -> None:
        if self._reader_error is None:
            self._reader_error = (code, message)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AgentSDKError(code, message))
        self._pending.clear()
        self._abandoned.clear()

    async def _read_responses(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while response := await process.stdout.readline():
                try:
                    wire = json.loads(response)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._fail_pending("invalid_response", "App Server返回了无效JSON")
                    return
                if not isinstance(wire, dict):
                    self._fail_pending("invalid_response", "App Server Response必须是JSON对象")
                    return
                response_id = wire.get("id")
                if isinstance(response_id, bool) or not isinstance(response_id, (str, int)):
                    self._fail_pending("invalid_response", "App Server Response缺少有效id")
                    return
                if response_id in self._abandoned:
                    self._abandoned.remove(response_id)
                    continue
                future = self._pending.pop(response_id, None)
                if future is None:
                    self._fail_pending("invalid_response", "App Server返回了未知Response id")
                    return
                if not future.done():
                    future.set_result(response)
        except asyncio.CancelledError:
            self._fail_pending("server_closed", "App Server响应读取已经停止")
            raise
        except (OSError, RuntimeError):
            self._fail_pending("server_closed", "App Server响应读取失败")
        else:
            self._fail_pending("server_closed", "App Server输出流已经关闭")

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            self.stderr_tail.extend(chunk)
            if len(self.stderr_tail) > 65_536:
                del self.stderr_tail[:-65_536]

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        request_id = self._frame_id(frame)
        process = await self._start()
        assert process.stdin is not None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        if request_id in self._pending or request_id in self._abandoned:
            raise AgentSDKError("duplicate_request_id", "存在相同id的未决Request")
        self._pending[request_id] = future
        try:
            async with self._write_lock:
                if self._closed:
                    raise AgentSDKError("server_closed", "App Server传输正在关闭")
                if self._reader_error is not None:
                    raise AgentSDKError(*self._reader_error)
                if process.returncode is not None:
                    raise AgentSDKError("server_closed", "App Server已经退出")
                try:
                    process.stdin.write(frame)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    raise AgentSDKError("server_closed", "App Server输入流写入失败") from None
            response = await asyncio.shield(future)
            return (response,)
        except asyncio.CancelledError:
            if self._pending.pop(request_id, None) is not None:
                future.cancel()
                self._abandoned.add(request_id)
            raise
        except BaseException:
            if self._pending.pop(request_id, None) is not None:
                future.cancel()
            raise

    async def notify(self, frame: bytes) -> None:
        process = await self._start()
        assert process.stdin is not None
        async with self._write_lock:
            if self._closed:
                raise AgentSDKError("server_closed", "App Server传输正在关闭")
            if self._reader_error is not None:
                raise AgentSDKError(*self._reader_error)
            if process.returncode is not None:
                raise AgentSDKError("server_closed", "App Server已经退出")
            try:
                process.stdin.write(frame)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise AgentSDKError("server_closed", "App Server输入流写入失败") from None

    async def close(self) -> None:
        async with self._close_lock:
            async with self._start_lock:
                if self._closed:
                    return
                self._closed = True
                process = self._process
            if process is None:
                return
            async with self._write_lock:
                if process.stdin is not None:
                    process.stdin.close()
                    try:
                        await process.stdin.wait_closed()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            try:
                async with asyncio.timeout(10):
                    await process.wait()
            except TimeoutError:
                process.terminate()
                try:
                    async with asyncio.timeout(5):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if self._reader_task is not None:
                await asyncio.gather(self._reader_task, return_exceptions=True)
            if self._stderr_task is not None:
                await self._stderr_task
            self._fail_pending("server_closed", "App Server传输已经关闭")


def _frame(message: ProtocolModel) -> bytes:
    return (
        json.dumps(
            message.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


class AgentClient:
    """Agent Protocol v1异步SDK；领域requestId由调用方控制并可安全复用。"""

    def __init__(
        self,
        transport: AgentTransport,
        *,
        client_instance_id: UUID | None = None,
        client_name: str = "harnessix-python-sdk",
        client_version: str = "0.8.0",
    ) -> None:
        self.transport = transport
        self.client_instance_id = client_instance_id or uuid4()
        self.client_name = client_name
        self.client_version = client_version
        self._sequence = 0
        self._initialize_lock = asyncio.Lock()
        self.initialized: InitializeResult | None = None

    async def __aenter__(self) -> Self:
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _send(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        self._sequence += 1
        request_id = self._sequence
        request = JsonRpcRequest(id=request_id, method=method, params=params)
        responses = await self.transport.exchange(_frame(request))
        if len(responses) != 1:
            raise AgentSDKError("invalid_response", "Request未收到唯一Response")
        try:
            wire = json.loads(responses[0])
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AgentSDKError("invalid_response", "Response不是有效JSON") from None
        if not isinstance(wire, dict) or wire.get("id") != request_id:
            raise AgentSDKError("invalid_response", "Response身份不匹配")
        if "error" in wire:
            error = wire.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            raw_path = data.get("path") if isinstance(data, dict) else None
            path = (
                tuple(
                    part
                    for part in raw_path
                    if isinstance(part, str) or isinstance(part, int) and not isinstance(part, bool)
                )
                if isinstance(raw_path, list)
                else ()
            )
            raise AgentSDKError(
                str(data.get("code", "protocol_error"))
                if isinstance(data, dict)
                else "protocol_error",
                str(error.get("message", "协议请求失败"))
                if isinstance(error, dict)
                else "协议请求失败",
                retryable=isinstance(data, dict) and data.get("retryable") is True,
                path=path,
            )
        if "result" not in wire:
            raise AgentSDKError("invalid_response", "Response缺少result或error")
        return cast(JsonValue, wire["result"])

    async def _notify(self, method: str, params: dict[str, JsonValue]) -> None:
        notification = JsonRpcNotification(method=method, params=params)
        await self.transport.notify(_frame(notification))

    async def initialize(self) -> InitializeResult:
        async with self._initialize_lock:
            if self.initialized is not None:
                return self.initialized
            params = InitializeParams(
                protocol_version=AGENT_PROTOCOL_VERSION,
                client_info=ClientInfo(name=self.client_name, version=self.client_version),
                client_instance_id=self.client_instance_id,
                capabilities=ClientCapabilities(item_deltas=True),
            )
            result = validate_server_output(
                InitializeResult,
                await self._send("initialize", params.model_dump(mode="json", by_alias=True)),
            )
            await self._notify("notifications/initialized", {})
            self.initialized = result
            return result

    async def create_thread(self, workspace: str, *, request_id: str) -> ThreadView:
        params = ThreadCreateParams(request_id=request_id, workspace=workspace)
        result = validate_server_output(
            ThreadResult,
            await self._send("thread/create", params.model_dump(mode="json", by_alias=True)),
        )
        return result.thread

    async def get_thread(self, thread_id: UUID) -> ThreadView:
        params = ThreadGetParams(thread_id=thread_id)
        result = validate_server_output(
            ThreadResult,
            await self._send("thread/get", params.model_dump(mode="json", by_alias=True)),
        )
        return result.thread

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        archived: bool | None = None,
    ) -> ThreadListResult:
        params = ThreadListParams(cursor=cursor, limit=limit, archived=archived)
        return validate_server_output(
            ThreadListResult,
            await self._send("thread/list", params.model_dump(mode="json", by_alias=True)),
        )

    async def resume_thread(self, thread_id: UUID) -> ThreadView:
        params = ThreadResumeParams(thread_id=thread_id)
        result = validate_server_output(
            ThreadResult,
            await self._send("thread/resume", params.model_dump(mode="json", by_alias=True)),
        )
        return result.thread

    async def fork_thread(
        self,
        source_thread_id: UUID,
        *,
        request_id: str,
        through_turn_id: UUID | None = None,
    ) -> ThreadView:
        params = ThreadForkParams(
            request_id=request_id,
            source_thread_id=source_thread_id,
            through_turn_id=through_turn_id,
        )
        result = validate_server_output(
            ThreadResult,
            await self._send("thread/fork", params.model_dump(mode="json", by_alias=True)),
        )
        return result.thread

    async def archive_thread(
        self,
        thread_id: UUID,
        *,
        request_id: str,
        reason: str | None = None,
    ) -> ThreadView:
        params = ThreadArchiveParams(
            request_id=request_id,
            thread_id=thread_id,
            reason=reason,
        )
        result = validate_server_output(
            ThreadResult,
            await self._send("thread/archive", params.model_dump(mode="json", by_alias=True)),
        )
        return result.thread

    async def start_turn(
        self,
        thread_id: UUID,
        prompt: str,
        *,
        request_id: str,
        budget: PublicBudget | None = None,
    ) -> TurnView:
        params = TurnStartParams(
            request_id=request_id,
            thread_id=thread_id,
            prompt=prompt,
            budget=budget,
        )
        result = validate_server_output(
            TurnResult,
            await self._send("turn/start", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def retry_turn(
        self,
        thread_id: UUID,
        source_turn_id: UUID,
        *,
        request_id: str,
        budget: PublicBudget | None = None,
    ) -> TurnView:
        params = TurnRetryParams(
            request_id=request_id,
            thread_id=thread_id,
            source_turn_id=source_turn_id,
            budget=budget,
        )
        result = validate_server_output(
            TurnResult,
            await self._send("turn/retry", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def resume_turn(
        self,
        thread_id: UUID,
        turn_id: UUID,
        *,
        request_id: str,
    ) -> TurnView:
        params = TurnResumeParams(
            request_id=request_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        result = validate_server_output(
            TurnResult,
            await self._send("turn/resume", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def cancel_turn(self, thread_id: UUID, turn_id: UUID, *, request_id: str) -> TurnView:
        params = TurnCancelParams(
            request_id=request_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        result = validate_server_output(
            TurnResult,
            await self._send("turn/cancel", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def steer_turn(
        self,
        thread_id: UUID,
        turn_id: UUID,
        text: str,
        *,
        request_id: str,
    ) -> TurnView:
        params = TurnSteerParams(
            request_id=request_id,
            thread_id=thread_id,
            turn_id=turn_id,
            text=text,
        )
        result = validate_server_output(
            TurnResult,
            await self._send("turn/steer", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def respond_approval(self, params: ApprovalRespondParams) -> TurnView:
        result = validate_server_output(
            TurnResult,
            await self._send("approval/respond", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def respond_question(self, params: QuestionRespondParams) -> TurnView:
        result = validate_server_output(
            TurnResult,
            await self._send("question/respond", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def replay_events(
        self, thread_id: UUID, *, after_cursor: int = 0, limit: int = 256
    ) -> EventsReplayResult:
        params = EventsReplayParams(
            thread_id=thread_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        return validate_server_output(
            EventsReplayResult,
            await self._send("events/replay", params.model_dump(mode="json", by_alias=True)),
        )

    async def read_artifact(
        self,
        thread_id: UUID,
        artifact_id: UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ArtifactPageResult:
        params = ArtifactReadParams(
            thread_id=thread_id,
            artifact_id=artifact_id,
            offset=offset,
            limit=limit,
        )
        return validate_server_output(
            ArtifactPageResult,
            await self._send("artifact/read", params.model_dump(mode="json", by_alias=True)),
        )

    async def next_events(
        self,
        thread_id: UUID,
        *,
        after_cursor: int = 0,
        wait_ms: int = 30_000,
        limit: int = 256,
    ) -> EventsNextResult:
        params = EventsNextParams(
            thread_id=thread_id,
            after_cursor=after_cursor,
            wait_ms=wait_ms,
            limit=limit,
        )
        return validate_server_output(
            EventsNextResult,
            await self._send("events/next", params.model_dump(mode="json", by_alias=True)),
        )

    async def watch_thread(
        self,
        thread_id: UUID,
        *,
        after_cursor: int = 0,
        wait_ms: int = 30_000,
        limit: int = 256,
    ) -> AsyncIterator[EventsNextResult]:
        """按权威Replay游标持续返回页面，并保留Delta缺口与超时语义。"""

        cursor = after_cursor
        while True:
            page = await self.next_events(
                thread_id,
                after_cursor=cursor,
                wait_ms=wait_ms,
                limit=limit,
            )
            cursor = max(cursor, page.replay.scanned_through)
            yield page

    async def close(self) -> None:
        await self.transport.close()
