"""待审批 Git 的专用 SQLite 原连接来源；事务与业务认证仍由调用方负责。

默认保留协作式路径边界；显式独占启动的原生模式另核验原 main FD 点时身份。
两种模式都不承诺 OS 原子 no-follow、历史 ABA 连续性或跨资源原子提交。
"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import local
from typing import NamedTuple

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_prepared_native_identity import (
    PreparedIdentityToken,
    attach_prepared_identity,
    check_prepared_identity,
    prepared_identity_backend,
)
from harnessix.sqlite_readonly import readonly_database

type _PhysicalPin = tuple[tuple[int, int], ...]


class _ConnectionSource(NamedTuple):
    path: Path
    before: _PhysicalPin
    after: _PhysicalPin
    task: asyncio.Task[object] | None
    native_identity: PreparedIdentityToken | None


_owned = local()


def _current_task() -> asyncio.Task[object] | None:
    """同步调用没有Task；准确任务身份不能用会被子任务继承的ContextVar代替。"""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _registered_prepared_connection(database: sqlite3.Connection) -> _ConnectionSource | None:
    """通用原始连接没有登记；已登记产品连接必须属于当前原Task，不能降为通用模式。"""
    issued: _ConnectionSource | None = getattr(_owned, "connections", {}).get(database)
    if issued is not None and _current_task() is not issued.task:
        raise _invalid()
    return issued


def _invalid() -> KernelError:
    return KernelError("git_prepared_link_host_invalid", "Git待审批账本原物理连接无效")


def _checkpoint(checkpoint: Callable[[], None] | None) -> None:
    if checkpoint is not None:
        checkpoint()


def _absolute_path(path: Path) -> Path:
    """不先 resolve 或折叠父目录遍历，以免掩盖符号链接的实际路径。"""
    if not isinstance(path, Path) or ".." in path.parts:
        raise _invalid()
    try:
        return path.absolute()
    except (OSError, ValueError):
        raise _invalid() from None


def _physical_pin(path: Path, checkpoint: Callable[[], None] | None) -> _PhysicalPin:
    """逐段 lstat 拒绝符号链接，保留所有实际父目录及普通文件的物理身份。"""
    identities = []
    for entry in (*reversed(path.parents), path):
        _checkpoint(checkpoint)
        try:
            observed = entry.lstat()
        except (OSError, ValueError):
            raise _invalid() from None
        expected = stat.S_ISREG if entry == path else stat.S_ISDIR
        if not expected(observed.st_mode):
            raise _invalid()
        identities.append((observed.st_dev, observed.st_ino))
    return tuple(identities)


def _database_path(
    database: sqlite3.Connection, path: Path, checkpoint: Callable[[], None] | None
) -> None:
    """路径报告只用于一致性校验，不能替代 context 私有登记的物理来源。"""
    _checkpoint(checkpoint)
    if type(database) is not sqlite3.Connection:
        raise _invalid()
    try:
        with closing(database.execute("PRAGMA database_list")) as cursor:
            attached = cursor.fetchall()
    except sqlite3.Error:
        raise _invalid() from None
    main = (0, "main", str(path))
    if attached not in ([main], [main, (1, "temp", "")]):
        raise _invalid()


def _require_alive(database: sqlite3.Connection) -> None:
    """文件检查点可能关闭原连接；在最后一次路径观察后再检查关闭状态。"""
    try:
        _ = database.in_transaction
    except sqlite3.Error:
        raise _invalid() from None


@contextmanager
def open_prepared_git_connection(
    path: Path, *, read_only: bool, checkpoint: Callable[[], None] | None = None
) -> Iterator[sqlite3.Connection]:
    """仅打开已存在普通文件；不建目录、不建库、不迁移、不创建业务认证或期限。

    只读复用原 mode=ro 端口；写连接使用 mode=rw 与显式事务模式。仅本 context
    可登记原 exact Connection，没有可由调用方提交的 witness 或注册入口。
    登记原Task身份；同线程的子Task、回调不能借用。正常或异常退出均撤销登记
    并关闭；不自动 COMMIT，未提交事务随关闭回滚。同步调用仍由原线程隔离。
    """
    _checkpoint(checkpoint)
    path = _absolute_path(path)
    if type(read_only) is not bool:
        raise _invalid()
    backend = prepared_identity_backend()
    before = _physical_pin(path, checkpoint)
    _checkpoint(checkpoint)
    native_identity = None
    try:
        database = (
            readonly_database(path)
            if read_only
            else sqlite3.connect(
                path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=0.1
            )
        )
    except (OSError, ValueError, sqlite3.Error):
        raise _invalid() from None
    try:
        if not read_only:
            _checkpoint(checkpoint)
            try:
                database.execute("PRAGMA foreign_keys=ON").close()
            except sqlite3.Error:
                raise _invalid() from None
        _database_path(database, path, checkpoint)
        after = _physical_pin(path, checkpoint)
        _require_alive(database)
        if before != after:
            raise _invalid()
        _checkpoint(checkpoint)
        native_identity = attach_prepared_identity(backend, database, before[-1])
        _checkpoint(checkpoint)
        check_prepared_identity(native_identity)
        _require_alive(database)
        connections = getattr(_owned, "connections", None)
        if connections is None:
            connections = {}
            _owned.connections = connections
        connections[database] = _ConnectionSource(
            path, before, after, _current_task(), native_identity
        )
        try:
            yield database
            _checkpoint(checkpoint)
        finally:
            del connections[database]
    finally:
        _close_connection(database, native_identity)


def _close_connection(
    database: sqlite3.Connection, native_identity: PreparedIdentityToken | None
) -> None:
    """先撤销 lease 再关闭原连接；已有取消、期限或首失败不被清理异常覆盖。"""
    primary_error = sys.exception()
    try:
        try:
            if native_identity is not None:
                native_identity.release()
        finally:
            database.close()
    except BaseException:
        if primary_error is None:
            raise


def require_prepared_git_connection(
    database: sqlite3.Connection, path: Path, *, checkpoint: Callable[[], None] | None = None
) -> None:
    """仅原线程及原Task中活跃context的原连接与原路径可进入，关闭或置换即拒绝。

    检查点直接传播原异常，不安装 SQL 回调、不处理事务、不替代 Owner/Key 校验。
    """
    _checkpoint(checkpoint)
    if type(database) is not sqlite3.Connection:
        raise _invalid()
    issued = getattr(_owned, "connections", {}).get(database)
    if issued is None:
        raise _invalid()
    if _current_task() is not issued.task:
        raise _invalid()
    _observe_source(database, path, issued, checkpoint)


def _observe_source(
    database: sqlite3.Connection,
    path: Path,
    issued: _ConnectionSource,
    checkpoint: Callable[[], None] | None,
) -> None:
    """只观察原连接来源，不授予当前Task使用原连接或进入SQL发布窗口的权限。"""
    if getattr(_owned, "connections", {}).get(database) is not issued:
        raise _invalid()
    original_path = issued.path
    if _absolute_path(path) != original_path or issued.before != issued.after:
        raise _invalid()
    _database_path(database, original_path, checkpoint)
    if _physical_pin(original_path, checkpoint) != issued.after:
        raise _invalid()
    _checkpoint(checkpoint)
    check_prepared_identity(issued.native_identity)
    _require_alive(database)


def _prepared_git_connection_observer(
    database: sqlite3.Connection, path: Path
) -> Callable[[], None]:
    """原Task先准入后签发只读来源检查点，可交给受管验证子Task，不签发SQL能力。"""
    require_prepared_git_connection(database, path)
    issued = _owned.connections[database]

    def observe() -> None:
        _observe_source(database, path, issued, None)

    return observe


def _prepared_git_connection_lifecycle_observer(database: sqlite3.Connection) -> Callable[[], None]:
    """原 Task 的纯段只检查登记和存活；路径及实际 SQL 来源仍由完整边界复核。"""
    issued = _registered_prepared_connection(database)
    if issued is None:
        raise _invalid()

    def observe() -> None:
        if _registered_prepared_connection(database) is not issued:
            raise _invalid()
        _require_alive(database)

    return observe


def _prepared_git_connection_registration_observer(
    database: sqlite3.Connection,
) -> Callable[[], None]:
    """原 Task 签发登记只读观察；不授予观察子 Task SQL 或事务权限。"""
    issued = _registered_prepared_connection(database)
    if issued is None:
        raise _invalid()

    def observe() -> None:
        if getattr(_owned, "connections", {}).get(database) is not issued:
            raise _invalid()
        _require_alive(database)

    return observe
