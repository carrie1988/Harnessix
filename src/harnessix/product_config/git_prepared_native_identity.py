"""待审批连接的显式原生装配；一旦要求原生身份，失败不得回落到路径检查。

仅独占进程启动期可初始化，不能在 SDK 握手或已有 Store 的进程中临时启用。
线程检查只能拒绝明显的晚启动；宿主仍须保证此前没有其他 SQLite 使用者。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from importlib import import_module
from typing import Protocol, cast

from harnessix.agent.errors import KernelError


class PreparedIdentityToken(Protocol):
    def check(self) -> bool: ...

    def release(self) -> None: ...


class _IdentityBackend(Protocol):
    BridgeError: type[Exception]

    def initialize_backend(self, connection: sqlite3.Connection, /) -> None: ...

    def attach_identity(
        self, connection: sqlite3.Connection, device: int, inode: int, /
    ) -> PreparedIdentityToken: ...


_state = "not_started"
_backend: _IdentityBackend | None = None
_connections_started = False


def _invalid() -> KernelError:
    return KernelError("git_prepared_link_host_invalid", "Git待审批原生连接身份不可用")


def _require_exclusive_startup() -> None:
    if (
        threading.current_thread() is not threading.main_thread()
        or threading.active_count() != 1
        or _connections_started
    ):
        raise _invalid()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise _invalid()


@contextmanager
def _extension_loading(database: sqlite3.Connection) -> Iterator[None]:
    """关闭临时扩展许可；清理失败不能覆盖 audit hook 的原取消或首失败。"""
    database.enable_load_extension(True)
    try:
        yield
    except BaseException:
        with suppress(BaseException):
            database.enable_load_extension(False)
        raise
    else:
        database.enable_load_extension(False)


def initialize_prepared_git_identity() -> None:
    """初始化独立组件一次；失败闭锁，必须重启宿主后再处理配置或资格问题。"""
    global _state, _backend
    if _state != "not_started":
        raise _invalid()
    _state = "loading"
    try:
        _require_exclusive_startup()
        try:
            backend = cast(_IdentityBackend, import_module("harnessix_sqlite_identity"))
        except (ImportError, OSError):
            raise _invalid() from None
        try:
            database = sqlite3.connect(":memory:")
            try:
                with _extension_loading(database):
                    backend.initialize_backend(database)
            except BaseException:
                with suppress(BaseException):
                    database.close()
                raise
            else:
                database.close()
        except (backend.BridgeError, sqlite3.Error):
            raise _invalid() from None
        _backend = backend
        _state = "ready"
    finally:
        if _state != "ready":
            _state = "failed"


def prepared_identity_backend() -> _IdentityBackend | None:
    """工厂首次使用后禁止更换来源模式；未启用模式不声称具备原生 FD 身份。"""
    global _connections_started
    _connections_started = True
    if _state == "not_started":
        return None
    if _state != "ready" or _backend is None:
        raise _invalid()
    return _backend


def attach_prepared_identity(
    backend: _IdentityBackend | None,
    database: sqlite3.Connection,
    physical_pin: tuple[int, int],
) -> PreparedIdentityToken | None:
    """使用打开前固定的 pin，不重新采样替代来源；只在装配期间允许扩展加载。"""
    if backend is None:
        return None
    token = None
    try:
        with _extension_loading(database):
            token = backend.attach_identity(database, *physical_pin)
        return token
    except BaseException as error:
        if token is not None:
            with suppress(BaseException):
                token.release()
        if isinstance(error, (backend.BridgeError, sqlite3.Error)):
            raise _invalid() from None
        raise


def check_prepared_identity(token: PreparedIdentityToken | None) -> None:
    """仅完整来源边界调用；不能放入无 I/O 的细粒度取消或生命周期检查。"""
    if token is None:
        return
    if _state != "ready" or _backend is None:
        raise _invalid()
    try:
        if token.check() is not True:
            raise _invalid()
    except (_backend.BridgeError, sqlite3.Error):
        raise _invalid() from None
