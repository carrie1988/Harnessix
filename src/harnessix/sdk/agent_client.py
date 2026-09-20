"""Python客户端SDK：通过可替换Transport驱动Agent Protocol会话。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, Self
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
)
from harnessix.sdk.errors import AgentSDKError as AgentSDKError
from harnessix.sdk.request import _frame, exchange_agent_request, require_replay_limit
from harnessix.sdk.response import _validate_result
from harnessix.sdk.subprocess import SubprocessAgentTransport as SubprocessAgentTransport

if TYPE_CHECKING:
    from harnessix.app_server.server import AgentProtocolServer


class AgentTransport(Protocol):
    """交换JSON-RPC请求与通知的SDK传输端口。"""

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
        self._initialize_failed = False
        self.initialized: InitializeResult | None = None

    async def __aenter__(self) -> Self:
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _send(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        self._sequence += 1
        return await exchange_agent_request(
            self.transport,
            method,
            params,
            self._sequence,
            self.initialized,
        )

    async def _notify(self, method: str, params: dict[str, JsonValue]) -> None:
        notification = JsonRpcNotification(method=method, params=params)
        await self.transport.notify(_frame(notification))

    async def initialize(self) -> InitializeResult:
        async with self._initialize_lock:
            if self.initialized is not None:
                return self.initialized
            if self._initialize_failed:
                raise AgentSDKError(
                    "handshake_failed",
                    "Agent Protocol握手未完成，当前连接不能复用",
                    retryable=True,
                )
            params = InitializeParams(
                protocol_version=AGENT_PROTOCOL_VERSION,
                client_info=ClientInfo(name=self.client_name, version=self.client_version),
                client_instance_id=self.client_instance_id,
                capabilities=ClientCapabilities(item_deltas=True),
            )
            try:
                result = _validate_result(
                    InitializeResult,
                    await self._send("initialize", params.model_dump(mode="json", by_alias=True)),
                )
                await self._notify("notifications/initialized", {})
            except BaseException:
                self._initialize_failed = True
                try:
                    await self.transport.close()
                except Exception:
                    pass
                raise
            self.initialized = result
            return result

    async def create_thread(self, workspace: str, *, request_id: str) -> ThreadView:
        params = ThreadCreateParams(request_id=request_id, workspace=workspace)
        result = _validate_result(
            ThreadResult,
            await self._send("thread/create", params.model_dump(mode="json", by_alias=True)),
        )
        return result.thread

    async def get_thread(self, thread_id: UUID) -> ThreadView:
        params = ThreadGetParams(thread_id=thread_id)
        result = _validate_result(
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
        return _validate_result(
            ThreadListResult,
            await self._send("thread/list", params.model_dump(mode="json", by_alias=True)),
        )

    async def resume_thread(self, thread_id: UUID) -> ThreadView:
        params = ThreadResumeParams(thread_id=thread_id)
        result = _validate_result(
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
        result = _validate_result(
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
        result = _validate_result(
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
        result = _validate_result(
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
        result = _validate_result(
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
        result = _validate_result(
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
        result = _validate_result(
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
        result = _validate_result(
            TurnResult,
            await self._send("turn/steer", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def respond_approval(self, params: ApprovalRespondParams) -> TurnView:
        result = _validate_result(
            TurnResult,
            await self._send("approval/respond", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def respond_question(self, params: QuestionRespondParams) -> TurnView:
        result = _validate_result(
            TurnResult,
            await self._send("question/respond", params.model_dump(mode="json", by_alias=True)),
        )
        return result.turn

    async def replay_events(
        self, thread_id: UUID, *, after_cursor: int = 0, limit: int = 256
    ) -> EventsReplayResult:
        require_replay_limit(self.initialized, limit)
        params = EventsReplayParams(
            thread_id=thread_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        return _validate_result(
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
        return _validate_result(
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
        require_replay_limit(self.initialized, limit)
        params = EventsNextParams(
            thread_id=thread_id,
            after_cursor=after_cursor,
            wait_ms=wait_ms,
            limit=limit,
        )
        return _validate_result(
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
