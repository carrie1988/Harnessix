"""Git 原连接借用实际 Runtime Thread 临界区；观察闭包不授予子 Task SQL 权限。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from threading import local
from types import MethodType
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.runtime_thread_lock import RuntimeThreadLock
from harnessix.agent.trusted_action_runtime import TrustedActionSessionRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.git_prefix_sql import (
    _git_prefix_caller_transaction_epoch,
    _git_prefix_transaction_scope,
    _require_git_prefix_caller_transaction_epoch,
)
from harnessix.product_config.git_prepared_link_connection import (
    _current_task,
    _registered_prepared_connection,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.router import TrustedActionRouter

_owned = local()


def _invalid() -> KernelError:
    return KernelError("git_runtime_thread_scope_invalid", "Git 原 Runtime Thread 临界区无效")


@dataclass(frozen=True, slots=True)
class _PreparedCommitRecord:
    """仅完整 prepare 成功后保留原操作资源；不是可复制或持久化的授权。"""

    epoch: tuple[object, int]
    resources: ExitStack
    check: Callable[[], None]


@dataclass(slots=True)
class _PreparedCommitState:
    """每个原 Runtime 窗口最多保留一个可复核的提交候选。"""

    record: _PreparedCommitRecord | None = None
    consuming: bool = False

    def discard(self, *, error: BaseException | None = None) -> None:
        record, self.record = self.record, None
        if record is not None:
            try:
                record.resources.close()
            except BaseException:
                if error is None:
                    raise


@dataclass(frozen=True, slots=True)
class _RuntimeThreadScope:
    """仅活动 context 登记原实例；既不是持久 Fence，也不是可复制的执行授权。"""

    thread_id: UUID
    task: asyncio.Task[object]
    router: TrustedActionRouter
    artifacts: SQLiteArtifactStore
    check: Callable[[], None]
    commit: _PreparedCommitState = field(default_factory=_PreparedCommitState)


def _runtime_check(runtime: AgentRuntime, thread_id: UUID) -> Callable[[], None]:
    """冻结原装配及同一次 acquire；同字节替身和释放后重新获取均不能复用。"""
    if type(runtime) is not AgentRuntime or type(thread_id) is not UUID:
        raise _invalid()
    runtime._ensure_open()
    runtime._require_thread_lock(thread_id)
    lock = runtime._locks[thread_id]
    if type(lock) is not RuntimeThreadLock:
        raise _invalid()
    observe_owner = lock.observe_owner()
    actions, session, artifacts = runtime._trusted_actions, runtime.store, runtime._artifacts
    if (
        type(actions) is not TrustedActionSessionRuntime
        or type(session) is not SQLiteSessionStore
        or type(artifacts) is not SQLiteArtifactStore
    ):
        raise _invalid()
    state = actions._state
    gateway, factory = state.gateway, state.lock
    if (
        type(gateway) is not RouterBackedAgentActionGateway
        or type(factory) is not MethodType
        or factory.__self__ is not runtime
        or factory.__func__ is not AgentRuntime._lock
        or state.store is not session
        or artifacts.session is not session
    ):
        raise _invalid()
    gateway_state = gateway._state
    router = gateway_state.router
    if type(router) is not TrustedActionRouter:
        raise _invalid()
    owner, token = runtime._owner, session._runtime_owner_token
    publication, guard = session._publication, artifacts._publication
    protection = runtime._public_output_protection
    if owner is None or token is None or publication is None or guard is None:
        raise _invalid()

    def check() -> None:
        # 可在受管验证子 Task 观察父 Task，但不把观察者登记为持有者。
        observe_owner()
        runtime._ensure_open()
        if (
            runtime._locks.get(thread_id) is not lock
            or runtime._owner is not owner
            or runtime.store is not session
            or runtime._artifacts is not artifacts
            or runtime._trusted_actions is not actions
            or actions._state is not state
            or state.gateway is not gateway
            or state.lock is not factory
            or state.store is not session
            or gateway._state is not gateway_state
            or gateway_state.router is not router
            or gateway._closed
            or artifacts.session is not session
            or artifacts._publication is not guard
            or session._publication is not publication
            or session._runtime_owner_token is not token
            or runtime._public_output_protection is not protection
        ):
            raise _invalid()

    check()
    return check


@contextmanager
def bind_prepared_git_runtime_thread(
    database: sqlite3.Connection, runtime: AgentRuntime, thread_id: UUID
) -> Iterator[None]:
    """由已持锁的原 Task 将活跃工厂连接绑定原 Runtime；不获取锁或控制事务。

    调用方必须先持原 Thread 锁，再打开原连接；本 context 必须覆盖 BEGIN、
    业务读写及 COMMIT/ROLLBACK。受管 U 子 Task 只能消费观察检查点，不能签发
    新窗口或使用原 SQL。异常退出撤销登记，不用末端检查遮盖原取消/首失败。
    """
    if (
        type(database) is not sqlite3.Connection
        or _registered_prepared_connection(database) is None
    ):
        raise _invalid()
    check = _runtime_check(runtime, thread_id)
    task = asyncio.current_task()
    if task is None:
        raise _invalid()
    actions, artifacts = runtime._trusted_actions, runtime._artifacts
    assert actions is not None and type(artifacts) is SQLiteArtifactStore
    gateway = actions._state.gateway
    assert type(gateway) is RouterBackedAgentActionGateway
    scopes: dict[sqlite3.Connection, _RuntimeThreadScope] | None = getattr(_owned, "scopes", None)
    if scopes is None:
        scopes = {}
        _owned.scopes = scopes
    if database in scopes:
        raise _invalid()
    scope = _RuntimeThreadScope(thread_id, task, gateway._state.router, artifacts, check)
    scopes[database] = scope
    try:
        with _git_prefix_transaction_scope(database):
            yield
            _require_scope(database).check()
    finally:
        try:
            scope.commit.discard(error=sys.exc_info()[1])
        finally:
            del scopes[database]


@contextmanager
def _prepared_git_commit_resources(database: sqlite3.Connection) -> Iterator[ExitStack]:
    """新 prepare 先复核旧候选，再原子替换资源；失败不能留下旧提交入口。"""
    scope = _require_scope(database)
    if scope.commit.consuming:
        raise _invalid()
    previous = scope.commit.record
    if previous is not None:
        try:
            # 已结束的事务只撤销旧候选；新 prepare 必须重新完成全部认证。
            if _git_prefix_caller_transaction_epoch(database) == previous.epoch:
                previous.check()
        finally:
            scope.commit.discard(error=sys.exc_info()[1])
    with ExitStack() as resources:
        yield resources


def _register_prepared_git_commit(
    database: sqlite3.Connection, resources: ExitStack, check: Callable[[], None]
) -> None:
    """仅内部 Ledger 成功出口转移原资源；不接受调用方提供的持久见证。"""
    scope = _require_scope(database)
    if scope.commit.record is not None or scope.commit.consuming:
        raise _invalid()
    epoch = _git_prefix_caller_transaction_epoch(database)
    scope.commit.record = _PreparedCommitRecord(epoch, resources.pop_all(), check)


@contextmanager
def prepared_git_commit_scope(database: sqlite3.Connection) -> Iterator[None]:
    """同步复核原 prepare 后才允许调用方提交；不提交、不回滚、不延长期限。

    验证的 SQL 窗口在交付控制前已结束。调用方须在本同步段直接 COMMIT，
    不得 await 或调用外部回调。退出仅撤销和回收；不将正常退出当作提交证明。
    裸 SQL 不受此端口拦截，默认 Git 写工具尚未装配。
    """
    scope = _require_scope(database)
    record = scope.commit.record
    if record is None or scope.commit.consuming:
        raise _invalid()
    scope.commit.consuming = True
    try:
        _require_git_prefix_caller_transaction_epoch(database, record.epoch)
        record.check()
        _require_scope(database)
        _require_git_prefix_caller_transaction_epoch(database, record.epoch)
        yield
    finally:
        try:
            scope.commit.discard(error=sys.exc_info()[1])
        finally:
            scope.commit.consuming = False


def _require_scope(database: sqlite3.Connection) -> _RuntimeThreadScope:
    """新的 SQL/业务窗口只准入签发它的原 Task，不接受 ContextVar 继承。"""
    scope: _RuntimeThreadScope | None = getattr(_owned, "scopes", {}).get(database)
    if scope is None or _current_task() is not scope.task:
        raise _invalid()
    if _registered_prepared_connection(database) is None:
        raise _invalid()
    scope.check()
    return scope


def _prepared_runtime_thread_observer(
    database: sqlite3.Connection, router: TrustedActionRouter, artifacts: SQLiteArtifactStore
) -> Callable[[], None]:
    """原 Task 先准入实际宿主，再签发仅检查原持锁生命周期的观察闭包。"""
    scope = _require_scope(database)
    if router is not scope.router or artifacts is not scope.artifacts:
        raise _invalid()

    def observe() -> None:
        if getattr(_owned, "scopes", {}).get(database) is not scope:
            raise _invalid()
        scope.check()

    return observe


def require_prepared_git_runtime_thread(database: sqlite3.Connection, thread_id: UUID) -> None:
    """待发布关联必须属于该临界区的实际 Thread；全集只读认证仍不筛掉其他行。"""
    scope = _require_scope(database)
    if type(thread_id) is not UUID or scope.thread_id != thread_id:
        raise _invalid()
