"""同一只读事务中的认证Thread与完整事件；元数据不授予执行权。"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, Thread
from harnessix.agent.reducer import replay
from harnessix.session.event_body_refs import EventBodyRef
from harnessix.session.sqlite_publication import authenticated_events
from harnessix.session.store_publication import SessionPublicationBinding, unproven

if TYPE_CHECKING:
    from harnessix.session.sqlite import SQLiteSessionStore


@dataclass(frozen=True, slots=True)
class AuthenticatedThreadHistory:
    """原读版本的普通历史聚合；无Key、签发或执行能力。"""

    thread: Thread
    events: tuple[AgentEvent, ...]
    # 旧两参构造仍可用于纯语义夹具；空引用不得作为原字节来源。
    body_refs: tuple[EventBodyRef, ...] = ()


class _OwnerCheckpointFailure(Exception):
    """只隔离Owner原OS/SQLite异常；真实回滚/关闭失败仍由原存储层优先分类。"""

    def __init__(self, original: OSError | sqlite3.Error) -> None:
        self.original = original
        super().__init__()


class _HistoryReadControl:
    """原取消和绝对期限贯穿SQL/事件；用户回调只在调用方事件循环运行。"""

    def __init__(
        self,
        store: SQLiteSessionStore,
        publication: SessionPublicationBinding,
        cancel: CancelToken,
        deadline: float,
        checkpoint: Callable[[], None] | None,
    ) -> None:
        self._store = store
        self._publication = publication
        self._cancel = cancel
        self._deadline = deadline
        self._owner_check = checkpoint
        self._parent_aborted = Event()
        self._interrupted: BaseException | None = None
        self._parent_check = parent_cancel_checkpointer(self._signals)

    def _signals(self) -> None:
        """SQLite工作线程仅检查停止信号，不执行Owner回调或访问事件循环。"""
        if self._interrupted is not None:
            raise self._interrupted
        if self._parent_aborted.is_set():
            raise asyncio.CancelledError
        self._cancel.checkpoint()
        if monotonic() >= self._deadline:
            raise KernelError("publication_history_timeout", "Session历史认证超时")

    def checkpoint(self) -> None:
        self._parent_check()
        if self._owner_check is not None:
            try:
                self._owner_check()
            except (OSError, sqlite3.Error) as error:
                # 原存储包装器只应分类驱动/文件系统失败，不能改写宿主检查点的原异常。
                raise _OwnerCheckpointFailure(error) from None
        # 原回调正常返回也可能已取消或更换Binding，交付前不可使用旧快照。
        self._parent_check()
        if self._store._publication is not self._publication:
            raise unproven()
        self._publication._ensure_open()

    def interrupt(self) -> int:
        """驱动回调返回中断信号；上层在storage包装前传播首次原异常。"""
        try:
            self._signals()
        except BaseException as error:
            if self._interrupted is None:
                self._interrupted = error
            return 1
        return 0

    def stop_for_parent_cancel(self) -> None:
        self._parent_aborted.set()

    def rethrow_interruption(self) -> None:
        if self._interrupted is not None:
            raise self._interrupted


async def read_authenticated_thread_history(
    store: SQLiteSessionStore,
    thread_id: UUID,
    *,
    cancel: CancelToken,
    deadline: float,
    checkpoint: Callable[[], None] | None,
) -> AuthenticatedThreadHistory:
    """单连接完整认证与语义重放；无DML、修复、补签或跨库原子承诺。"""
    if (
        type(thread_id) is not UUID
        or not isinstance(cancel, CancelToken)
        or type(deadline) is not float
        or not math.isfinite(deadline)
        or (checkpoint is not None and not callable(checkpoint))
    ):
        raise KernelError("invalid_history_request", "历史读取参数无效")
    publication = store._publication
    if publication is None:
        raise unproven()
    control = _HistoryReadControl(store, publication, cancel, deadline, checkpoint)
    try:
        control.checkpoint()
        return await _read_in_transaction(store, thread_id, publication, control)
    except _OwnerCheckpointFailure as error:
        raise error.original from None


async def _read_in_transaction(
    store: SQLiteSessionStore,
    thread_id: UUID,
    publication: SessionPublicationBinding,
    control: _HistoryReadControl,
) -> AuthenticatedThreadHistory:
    """原Header认证在BEGIN内；连接退出后才交付候选或传播原Owner异常。"""
    async with store._connection(authenticate=False, read_only=True) as database:
        await database.set_progress_handler(control.interrupt, 1000)
        try:
            control.checkpoint()
            await database.execute("BEGIN")
            thread = await store._snapshot(database, thread_id)
            control.checkpoint()
            if thread is None:
                raise KernelError("thread_not_found", "Thread不存在")
            body_refs: list[EventBodyRef] = []
            events = await authenticated_events(
                database,
                publication,
                thread_id,
                0,
                history_checkpoint=control.checkpoint,
                _body_refs=body_refs,
            )
            control.checkpoint()
            if thread != replay(events):
                raise unproven()
            candidate = AuthenticatedThreadHistory(thread, tuple(events), tuple(body_refs))
            control.checkpoint()
        except sqlite3.Error:
            control.rethrow_interruption()
            raise
        except asyncio.CancelledError:
            # 先停止原工作线程，再借用原连接回滚/关闭的取消结算。
            control.stop_for_parent_cancel()
            raise
    control.checkpoint()
    return candidate
