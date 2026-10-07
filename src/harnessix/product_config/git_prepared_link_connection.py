"""待审批 Git 的专用 SQLite 原连接来源；事务与业务认证仍由调用方负责。

打开前后的路径及 dev/inode 检查是协作式私有状态边界：拒绝打开 A 后将路径
替换为 B 的旧连接，不承诺恶意并发换回路径时的 OS 原子 no-follow 或 FD 认证。
"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import local

from harnessix.agent.errors import KernelError
from harnessix.sqlite_readonly import readonly_database

type _PhysicalPin = tuple[tuple[int, int], ...]

_owned = local()


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
    正常或异常退出均撤销登记并关闭；不自动 COMMIT，未提交事务随关闭回滚。
    """
    _checkpoint(checkpoint)
    path = _absolute_path(path)
    if type(read_only) is not bool:
        raise _invalid()
    before = _physical_pin(path, checkpoint)
    _checkpoint(checkpoint)
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
        connections = getattr(_owned, "connections", None)
        if connections is None:
            connections = {}
            _owned.connections = connections
        connections[database] = (path, before, after)
        try:
            yield database
            _checkpoint(checkpoint)
        finally:
            del connections[database]
    finally:
        database.close()


def require_prepared_git_connection(
    database: sqlite3.Connection, path: Path, *, checkpoint: Callable[[], None] | None = None
) -> None:
    """仅当前线程中原活跃 context 的原连接与原路径可进入，关闭或置换即拒绝。

    检查点直接传播原异常，不安装 SQL 回调、不处理事务、不替代 Owner/Key 校验。
    """
    _checkpoint(checkpoint)
    if type(database) is not sqlite3.Connection:
        raise _invalid()
    issued = getattr(_owned, "connections", {}).get(database)
    if issued is None:
        raise _invalid()
    original_path, before, after = issued
    if _absolute_path(path) != original_path or before != after:
        raise _invalid()
    _database_path(database, original_path, checkpoint)
    if _physical_pin(original_path, checkpoint) != after:
        raise _invalid()
    _require_alive(database)
