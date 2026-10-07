"""同步末端读的操作局部控制；不改变原 Store、认证算法或共享回调。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from harnessix.agent.errors import KernelError
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass(slots=True)
class _TerminalRead:
    """仅持有本次原读取资源；active 栅栏拒绝复制上下文的延迟使用。"""

    owners: tuple[object, object] = field(repr=False)
    read_blob: Callable[[str], bytes] = field(repr=False)
    checkpoint: Callable[[], None] = field(repr=False)
    thread: int
    task: object | None = field(repr=False)
    active: bool = True

    def read(self, digest: str) -> bytes:
        """父闭包沿原严格 CAS IO 读取，控制异常保留来源而非改判正文损坏。"""
        self._protected_check()
        body = self.read_blob(digest)
        self._protected_check()
        return body

    def check(self) -> None:
        if not self.active or self.thread != threading.get_ident() or self.task is not _task():
            raise KernelError("terminal_read_scope_invalid", "同步末端读取作用域已经失效")
        self.checkpoint()

    def _protected_check(self) -> None:
        try:
            self.check()
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None


_CURRENT: ContextVar[_TerminalRead | None] = ContextVar("harnessix_terminal_read", default=None)


def _scope(owner: object) -> _TerminalRead | None:
    scope = _CURRENT.get()
    if scope is None or not any(owner is original for original in scope.owners):
        return None
    return scope


def run_store_read_checkpoint(owner: object, original: Callable[[], None] | None) -> None:
    """正常 SDK 保留原回调；末端原资源只消费本次内部控制。"""
    scope = _scope(owner)
    if scope is not None:
        scope.check()
    elif original is not None:
        original()


def terminal_parent_reader(owner: object) -> Callable[[str], bytes] | None:
    """Audit 末端父闭包只借原严格 CAS，不调用共享构造 Reader。"""
    scope = _scope(owner)
    return None if scope is None else scope.read


def require_store_write_allowed(owner: object) -> None:
    """末端读取作用域不允许同一原资源转为业务写入。"""
    if _scope(owner) is not None:
        raise KernelError("terminal_read_write_denied", "同步末端读取作用域不接受写入")


def require_terminal_read_scope(transactions: object, audit: object) -> None:
    """末端业务复核只能在同一原资源的活跃同步作用域内执行。"""
    scope = _scope(transactions)
    if scope is None or _scope(audit) is not scope:
        raise KernelError("terminal_read_scope_invalid", "同步末端读取作用域缺失或不匹配")
    scope.check()


@contextmanager
def terminal_read_scope(
    transactions: object,
    audit: object,
    read_blob: Callable[[str], bytes],
    checkpoint: Callable[[], None],
) -> Iterator[None]:
    """全集同步使用同一内部控制；禁止嵌套，退出后恢复正常 SDK 行为。"""
    if _CURRENT.get() is not None or transactions is audit:
        raise KernelError("terminal_read_scope_invalid", "同步末端读取作用域不能重入")
    scope = _TerminalRead(
        (transactions, audit), read_blob, checkpoint, threading.get_ident(), _task()
    )
    token = _CURRENT.set(scope)
    try:
        checkpoint()
        yield
        checkpoint()
    finally:
        scope.active = False
        _CURRENT.reset(token)
