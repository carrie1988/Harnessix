"""启动Agent Server时的配置切换前提：读取工作区当前生效的配置指针，让用户改过的配置生效。

Agent Server按CAS切换工作区的活动配置（ADR 0075第2.5节）：配置文件变了而调用方不给旧指针时，
Server以`product_config_conflict`退出。产品入口此前从不提供旧指针，用户用`configure`换了模型后，
用过的工作区就再也打不开（界面只显示server_closed，0.10.0b6在macOS实测）。入口在启动前只读取
当前指针并原样作为前提交出；读取与切换之间若有其他进程改动，Server仍按CAS拒绝。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore

Activation = tuple[str | None, str | None, str | None]
_RUNTIME_CONFIG_DATABASE = "product-config.db"
_FLAGS = (
    "--expected-active-sha256",
    "--expected-active-profile",
    "--expected-active-action-sha256",
)


def activation_arguments(runtime_state: Path, explicit: Activation) -> tuple[str, ...]:
    """Agent Server命令行的切换前提参数；没有前提的项不出现。"""

    expected = expected_activation(runtime_state, explicit)
    return tuple(
        part
        for flag, value in zip(_FLAGS, expected, strict=True)
        if value is not None
        for part in (flag, value)
    )


def expected_activation(runtime_state: Path, explicit: Activation) -> Activation:
    """调用方显式给出任一前提时原样使用；否则用工作区当前生效的（配置、Profile、Action配置）。"""

    if any(value is not None for value in explicit):
        return explicit
    return current_activation(runtime_state)


def current_activation(runtime_state: Path) -> Activation:
    """只读当前活动指针；没有状态库或读不出时返回空前提，交由Server按原规则判断。"""

    path = runtime_state / _RUNTIME_CONFIG_DATABASE
    if not path.is_file():
        return None, None, None
    try:
        store = SQLiteProductRuntimeConfigStore(path, read_only=True)
    except (KernelError, sqlite3.Error, OSError):
        return None, None, None
    try:
        active = _read(store.active)
        action = _read(store.active_action)
    finally:
        store.close()
    config_sha256, profile_id = active if active is not None else (None, None)
    return config_sha256, profile_id, action


def _read[T](reader: Callable[[], T]) -> T | None:
    try:
        return reader()
    except (KernelError, sqlite3.Error):
        return None
