from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Protocol, Self, cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from harnessix.app_server.server import AgentProtocolServer
from harnessix.protocol.contracts import (
    AGENT_PROTOCOL_VERSION,
    ApprovalRespondParams,
    ClientInfo,
    EventsReplayParams,
    EventsReplayResult,
    InitializeParams,
    InitializeResult,
    JsonRpcNotification,
    JsonRpcRequest,
    ProtocolModel,
    PublicBudget,
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
    TurnView,
    validate_server_output,
)


class AgentSDKError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


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
    """单进程stdio传输；一个reader/writer锁保证响应身份不串线。"""

    def __init__(self, command: Sequence[str]) -> None:
        if not command:
            raise ValueError("App Server命令不能为空")
        self.command = tuple(command)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self.stderr_tail = bytearray()

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is None:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(self._process), name="harnessix-sdk-stderr"
            )
        return self._process

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            self.stderr_tail.extend(chunk)
            if len(self.stderr_tail) > 65_536:
                del self.stderr_tail[:-65_536]

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        async with self._lock:
            process = await self._start()
            assert process.stdin is not None and process.stdout is not None
            if process.returncode is not None:
                raise AgentSDKError("server_closed", "App Server已经退出")
            process.stdin.write(frame)
            await process.stdin.drain()
            response = await process.stdout.readline()
            if not response:
                raise AgentSDKError("server_closed", "App Server未返回响应")
            return (response,)

    async def notify(self, frame: bytes) -> None:
        async with self._lock:
            process = await self._start()
            assert process.stdin is not None
            if process.returncode is not None:
                raise AgentSDKError("server_closed", "App Server已经退出")
            process.stdin.write(frame)
            await process.stdin.drain()

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
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
        if self._stderr_task is not None:
            await self._stderr_task


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
        self.initialized: InitializeResult | None = None

    async def __aenter__(self) -> Self:
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _send(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        self._sequence += 1
        request = JsonRpcRequest(id=self._sequence, method=method, params=params)
        responses = await self.transport.exchange(_frame(request))
        if len(responses) != 1:
            raise AgentSDKError("invalid_response", "Request未收到唯一Response")
        try:
            wire = json.loads(responses[0])
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AgentSDKError("invalid_response", "Response不是有效JSON") from None
        if not isinstance(wire, dict) or wire.get("id") != self._sequence:
            raise AgentSDKError("invalid_response", "Response身份不匹配")
        if "error" in wire:
            error = wire.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            raise AgentSDKError(
                str(data.get("code", "protocol_error"))
                if isinstance(data, dict)
                else "protocol_error",
                str(error.get("message", "协议请求失败"))
                if isinstance(error, dict)
                else "协议请求失败",
                retryable=isinstance(data, dict) and data.get("retryable") is True,
            )
        if "result" not in wire:
            raise AgentSDKError("invalid_response", "Response缺少result或error")
        return cast(JsonValue, wire["result"])

    async def _notify(self, method: str, params: dict[str, JsonValue]) -> None:
        notification = JsonRpcNotification(method=method, params=params)
        await self.transport.notify(_frame(notification))

    async def initialize(self) -> InitializeResult:
        if self.initialized is not None:
            return self.initialized
        params = InitializeParams(
            protocol_version=AGENT_PROTOCOL_VERSION,
            client_info=ClientInfo(name=self.client_name, version=self.client_version),
            client_instance_id=self.client_instance_id,
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

    async def respond_approval(self, params: ApprovalRespondParams) -> TurnView:
        result = validate_server_output(
            TurnResult,
            await self._send("approval/respond", params.model_dump(mode="json", by_alias=True)),
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

    async def close(self) -> None:
        await self.transport.close()
