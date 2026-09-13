"""客户端连接代际、冷暖恢复与稳定命令身份编排。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from harnessix.product_ui.contracts import ClientStateV1, command_request_id
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.projection import (
    ProductViewState,
    apply_events_next,
    apply_replay_page,
    cold_product_view,
    refresh_thread_snapshot,
)
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.contracts import ThreadListResult, ThreadView
from harnessix.sdk import AgentClient, AgentSDKError
from harnessix.sdk.agent_client import AgentTransport


class ConnectionPhase(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    HYDRATING = "hydrating"
    READY = "ready"
    BROKEN = "broken"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ProductConnection:
    """当前连接代际、生命周期阶段和最后稳定错误。"""

    generation: int = 0
    phase: ConnectionPhase = ConnectionPhase.DISCONNECTED
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedClientCommand:
    """发送结果不明确时可跨连接代际复用的单个逻辑命令身份。"""

    client_instance_id: UUID
    request_id: str
    sequence: int
    allocated_state_revision: int


class AgentTransportFactory(Protocol):
    def __call__(self) -> AgentTransport: ...


_CONNECTION_FAILURES = frozenset(
    {
        "handshake_failed",
        "invalid_response",
        "server_closed",
        "server_start_failed",
    }
)


def _connection_after_error(
    connection: ProductConnection,
    error: AgentSDKError | ProductUIError,
) -> ProductConnection:
    if isinstance(error, AgentSDKError) and error.code not in _CONNECTION_FAILURES:
        return connection
    return ProductConnection(connection.generation, ConnectionPhase.BROKEN, error.code)


class RecoverableAgentSession:
    """管理一个可重连Agent SDK以及同进程可续传的Thread投影。"""

    def __init__(
        self,
        state_store: ClientStateStore,
        transport_factory: AgentTransportFactory,
    ) -> None:
        self._state_store = state_store
        self._transport_factory = transport_factory
        self._client: AgentClient | None = None
        self._views: dict[UUID, ProductViewState] = {}
        self._operation_lock = asyncio.Lock()
        self.connection = ProductConnection()

    async def connect(self) -> ProductConnection:
        """关闭旧代际并建立完整握手的新代际；半握手连接绝不复用。"""

        async with self._operation_lock:
            if self.connection.phase is ConnectionPhase.CLOSED:
                raise ProductUIError("client_session_closed", "产品客户端会话已关闭")
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:
                    self.connection = ProductConnection(
                        generation=self.connection.generation,
                        phase=ConnectionPhase.BROKEN,
                        last_error_code="connection_close_failed",
                    )
                    raise ProductUIError(
                        "connection_close_failed", "旧Agent连接无法安全关闭"
                    ) from None
                finally:
                    self._client = None
            self._state_store.set_clean_shutdown(False)
            generation = self.connection.generation + 1
            self.connection = ProductConnection(generation, ConnectionPhase.CONNECTING)
            try:
                transport = self._transport_factory()
                client = AgentClient(
                    transport,
                    client_instance_id=self._state_store.state().client_instance_id,
                    client_name="harnessix-code-tui",
                    client_version="0.9.1",
                )
                self.connection = ProductConnection(generation, ConnectionPhase.HANDSHAKING)
                await client.initialize()
            except AgentSDKError as error:
                self.connection = ProductConnection(generation, ConnectionPhase.BROKEN, error.code)
                raise
            except Exception:
                self.connection = ProductConnection(
                    generation,
                    ConnectionPhase.BROKEN,
                    "connection_start_failed",
                )
                raise ProductUIError(
                    "connection_start_failed", "Agent连接无法建立", retryable=True
                ) from None
            self._client = client
            self.connection = ProductConnection(generation, ConnectionPhase.READY)
            return self.connection

    def prepare_command(self) -> PreparedClientCommand:
        """先持久消费序列，再把可重放身份交给调用方。"""

        self._ready_client()
        allocation = self._state_store.allocate_command_id()
        state = self._state_store.state()
        return PreparedClientCommand(
            client_instance_id=state.client_instance_id,
            request_id=allocation.request_id,
            sequence=allocation.sequence,
            allocated_state_revision=allocation.state_revision,
        )

    async def execute_prepared[Result](
        self,
        command: PreparedClientCommand,
        operation: Callable[[AgentClient, PreparedClientCommand], Awaitable[Result]],
    ) -> Result:
        """执行已准备命令；失败不生成新ID，重连后由调用方显式重放同一对象。"""

        async with self._operation_lock:
            client = self._ready_client()
            state = self._state_store.state()
            try:
                valid_identity = (
                    command.client_instance_id == state.client_instance_id
                    and command.request_id
                    == command_request_id(command.client_instance_id, command.sequence)
                    and 1 <= command.allocated_state_revision <= state.state_revision
                    and command.sequence < state.next_command_sequence
                )
            except (TypeError, ValueError):
                valid_identity = False
            if not valid_identity:
                raise ProductUIError("client_command_invalid", "命令身份不属于当前客户端")
            try:
                return await operation(client, command)
            except (AgentSDKError, ProductUIError) as error:
                self.connection = _connection_after_error(self.connection, error)
                raise

    async def execute_query[Result](
        self,
        operation: Callable[[AgentClient], Awaitable[Result]],
    ) -> Result:
        """串行执行不消费Command ID的只读查询，并保留连接失败语义。"""

        async with self._operation_lock:
            client = self._ready_client()
            try:
                return await operation(client)
            except (AgentSDKError, ProductUIError) as error:
                self.connection = _connection_after_error(self.connection, error)
                raise

    async def hydrate_thread(self, thread_id: UUID) -> ProductViewState:
        """冷启动从0重建；同一Session已有完整投影时从内存游标暖续传。"""

        async with self._operation_lock:
            client = self._ready_client()
            generation = self.connection.generation
            self.connection = ProductConnection(generation, ConnectionPhase.HYDRATING)
            try:
                snapshot = await client.get_thread(thread_id)
                existing = self._views.get(thread_id)
                view = (
                    cold_product_view(snapshot)
                    if existing is None
                    else refresh_thread_snapshot(existing, snapshot)
                )
                assert client.initialized is not None
                limit = min(256, client.initialized.limits.max_replay_events)
                while True:
                    before = view.durable_cursor
                    page = await client.replay_events(
                        thread_id,
                        after_cursor=before,
                        limit=limit,
                    )
                    candidate = apply_replay_page(view, page)
                    if page.has_more and candidate.durable_cursor <= before:
                        raise ProductUIError(
                            "projection_replay_stalled", "Replay分页未推进持久游标"
                        )
                    self._state_store.advance_cursor(thread_id, candidate.durable_cursor)
                    view = candidate
                    if not page.has_more:
                        break
                if view.durable_cursor < snapshot.cursor:
                    raise ProductUIError(
                        "projection_replay_incomplete", "Replay未覆盖Thread Snapshot游标"
                    )
                self._state_store.select_thread(thread_id)
                self._views[thread_id] = view
            except (AgentSDKError, ProductUIError) as error:
                self.connection = _connection_after_error(self.connection, error)
                raise
            self.connection = ProductConnection(generation, ConnectionPhase.READY)
            return view

    def client_state(self) -> ClientStateV1:
        """返回重新校验过的客户端持久状态，不暴露Store写入口。"""

        return self._state_store.state()

    def clear_selected_thread(self) -> None:
        """清除已经不在当前Workspace活动列表中的本地选择。"""

        self._state_store.select_thread(None)

    async def list_threads_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> ThreadListResult:
        """在当前连接代际内读取一页未归档Thread。"""

        async with self._operation_lock:
            client = self._ready_client()
            try:
                return await client.list_threads(cursor=cursor, limit=limit, archived=False)
            except AgentSDKError as error:
                self.connection = _connection_after_error(self.connection, error)
                raise

    async def resume_thread(self, thread_id: UUID) -> ThreadView:
        """恢复服务端Thread驱动；该协议操作本身由Agent Runtime保证可重复。"""

        async with self._operation_lock:
            client = self._ready_client()
            try:
                return await client.resume_thread(thread_id)
            except AgentSDKError as error:
                self.connection = _connection_after_error(self.connection, error)
                raise

    async def poll_thread(
        self,
        thread_id: UUID,
        *,
        wait_ms: int = 30_000,
    ) -> ProductViewState:
        """读取一页持久事件与临时Delta，只有持久扫描位置写入ClientState。"""

        async with self._operation_lock:
            client = self._ready_client()
            view = self._views.get(thread_id)
            if view is None:
                raise ProductUIError("projection_not_hydrated", "Thread尚未完成投影恢复")
            assert client.initialized is not None
            limit = min(256, client.initialized.limits.max_replay_events)
            try:
                page = await client.next_events(
                    thread_id,
                    after_cursor=view.durable_cursor,
                    wait_ms=wait_ms,
                    limit=limit,
                )
                candidate = apply_events_next(view, page)
                self._state_store.advance_cursor(thread_id, candidate.durable_cursor)
                self._views[thread_id] = candidate
                return candidate
            except (AgentSDKError, ProductUIError) as error:
                self.connection = _connection_after_error(self.connection, error)
                raise

    def view(self, thread_id: UUID) -> ProductViewState | None:
        return self._views.get(thread_id)

    def _ready_client(self) -> AgentClient:
        if self.connection.phase is not ConnectionPhase.READY or self._client is None:
            raise ProductUIError("connection_not_ready", "Agent连接尚未就绪", retryable=True)
        return self._client

    async def close(self) -> None:
        async with self._operation_lock:
            if self.connection.phase is ConnectionPhase.CLOSED:
                return
            generation = self.connection.generation
            self.connection = ProductConnection(generation, ConnectionPhase.CLOSING)
            client = self._client
            self._client = None
            try:
                if client is not None:
                    await client.close()
                self._state_store.set_clean_shutdown(True)
            except Exception:
                self.connection = ProductConnection(
                    generation,
                    ConnectionPhase.BROKEN,
                    "connection_close_failed",
                )
                raise ProductUIError("connection_close_failed", "Agent连接无法安全关闭") from None
            self.connection = ProductConnection(generation, ConnectionPhase.CLOSED)

    async def __aenter__(self) -> RecoverableAgentSession:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
