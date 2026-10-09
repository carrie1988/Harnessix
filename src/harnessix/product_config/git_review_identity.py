"""Review 连接来源的两种生命周期：原 Audit 随宿主持有，鲜读随单次查询持有。

仅显式原生启动时检查实际 main 身份，不授予 Owner、SQL 或 Git 写权限。
"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import NamedTuple

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_native_identity as native
from harnessix.trusted_actions.store import SQLiteActionAuditStore


def _audit_file_identity(path: Path) -> tuple[int, int]:
    """固定已有Audit普通文件，拒绝缺失、符号链接及可观察替换；不宣称FD认证。"""
    try:
        value = path.lstat()
    except OSError:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件不可用") from None
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件不可用")
    return value.st_dev, value.st_ino


@contextmanager
def _observe_review_connection(
    database: sqlite3.Connection, identity: tuple[int, int]
) -> Iterator[Callable[[], None]]:
    """借连接核验既有 pin；每条连接只绑定一次，作用域拥有令牌但不拥有连接。"""
    try:
        backend = native.prepared_identity_backend()
        token = native.attach_prepared_identity(backend, database, identity)
    except KernelError as error:
        if error.code == "git_prepared_link_host_invalid":
            raise KernelError(
                "git_action_review_host_invalid", "Git审阅原生连接身份不可用"
            ) from None
        raise
    active = True

    def check() -> None:
        if not active:
            raise KernelError("git_action_review_host_invalid", "Git审阅连接观察已经结束")
        try:
            native.check_prepared_identity(token)
        except KernelError as error:
            if error.code == "git_prepared_link_host_invalid":
                raise KernelError(
                    "git_action_review_host_invalid", "Git审阅原生连接身份不可用"
                ) from None
            raise

    failed = False
    try:
        check()
        yield check
    except BaseException:
        failed = True
        raise
    finally:
        active = False
        # 先撤销闭包，再释放令牌；清理错误不能覆盖 Owner、取消或超时的首失败。
        try:
            if token is not None:
                token.release()
        except BaseException as error:
            if not failed:
                if backend is not None and isinstance(error, (backend.BridgeError, sqlite3.Error)):
                    raise KernelError(
                        "git_action_review_host_invalid", "Git审阅原生连接身份不可用"
                    ) from None
                raise


class _AuditIdentity(NamedTuple):
    database: sqlite3.Connection
    path: Path
    pin: tuple[int, int]
    check: Callable[[], None]


_audit_connections = local()


def _invalid() -> KernelError:
    return KernelError("git_action_review_host_invalid", "Git审阅原Audit连接身份不可用")


def _native_mode() -> bool:
    try:
        return native.prepared_identity_backend() is not None
    except KernelError as error:
        if error.code == "git_prepared_link_host_invalid":
            raise _invalid() from None
        raise


@contextmanager
def bind_product_audit_identity(audit: SQLiteActionAuditStore) -> Iterator[None]:
    """宿主在 Store 打开后进入，退出先撤销登记及令牌，再由外层关闭原连接。"""
    if not _native_mode():
        yield
        return
    if type(audit) is not SQLiteActionAuditStore or audit._closed is not False:
        raise _invalid()
    database, path = audit._db, audit._path
    if type(database) is not sqlite3.Connection or not isinstance(path, Path):
        raise _invalid()
    registrations: dict[int, _AuditIdentity | None] | None = getattr(
        _audit_connections, "registrations", None
    )
    if registrations is None:
        registrations = {}
        _audit_connections.registrations = registrations
    # 固定整数键，退出不调用可能已被替换的 Store __hash__。作用域保活原 Store。
    audit_id = id(audit)
    if audit_id in registrations:
        raise _invalid()
    pin = _audit_file_identity(path)
    # pending 也占用槽位，拒绝 attach 的 audit hook 重入；尚未签发观察能力。
    registrations[audit_id] = None
    try:
        with _observe_review_connection(database, pin) as check:
            try:
                if (
                    type(audit) is not SQLiteActionAuditStore
                    or audit._db is not database
                    or audit._path is not path
                    or audit._closed is not False
                ):
                    raise _invalid()
                issued = _AuditIdentity(database, path, pin, check)
                registrations[audit_id] = issued
                yield
            finally:
                registrations.pop(audit_id, None)
    finally:
        # 进入令牌作用域失败时也必须清掉 pending，不能留下半注册宿主。
        registrations.pop(audit_id, None)


def original_audit_observer(
    audit: SQLiteActionAuditStore,
    database: sqlite3.Connection,
    path: Path,
    identity: tuple[int, int],
) -> Callable[[], None]:
    """借用本线程宿主签发的来源观察，不转移 Task 的 SQL 或 Owner 权限。"""
    if not _native_mode():
        return lambda: None
    if type(audit) is not SQLiteActionAuditStore:
        raise _invalid()
    issued: _AuditIdentity | None = getattr(_audit_connections, "registrations", {}).get(id(audit))
    if (
        issued is None
        or type(issued) is not _AuditIdentity
        or issued.database is not database
        or issued.path is not path
        or issued.pin != identity
    ):
        raise _invalid()
    check_identity = issued.check

    def check() -> None:
        if (
            type(audit) is not SQLiteActionAuditStore
            or getattr(_audit_connections, "registrations", {}).get(id(audit)) is not issued
            or audit._closed is not False
            or audit._db is not database
            or audit._path is not path
        ):
            raise _invalid()
        check_identity()

    return check
