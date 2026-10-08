"""在原创建 Task 传递确切 Git 控制，保留原生转换边界的一层异常标记。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import get_ident

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def protected_git_control(
    checkpoint: Callable[[], None],
    full: Callable[[], None],
    *,
    mark_error: Callable[[BaseException], UpstreamCheckpointError] = UpstreamCheckpointError,
) -> Callable[[], None]:
    """原创建 Task 才可传递局部检查；保留跨解析边界的一层控制异常标记。"""
    if type(checkpoint) is not GitAuthenticationControl:
        return full
    origin = GitAuthenticationControl._binding(checkpoint)
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if origin[2] is not task or origin[3] != get_ident():
        return full

    def verify_parent() -> None:
        try:
            if type(checkpoint) is not GitAuthenticationControl:
                raise KernelError("git_authentication_control_invalid", "Git控制创建绑定已改变")
            GitAuthenticationControl._binding(checkpoint, origin)
        except BaseException as error:
            raise mark_error(error) from None

    def local() -> None:
        verify_parent()
        try:
            origin[0]()
        except BaseException as error:
            raise mark_error(error) from None

    def authenticate() -> None:
        verify_parent()
        full()

    # 不扩大身份或重绑归属；只有原 Task／线程在本同步入口派生控制。
    return GitAuthenticationControl(local, authenticate)


@contextmanager
def git_checkpoint_boundary(checkpoint: Callable[[], None]) -> Iterator[Callable[[], None]]:
    """只解本边界创建的异常对象；原观察异常即使已有标记，也保持原对象。"""
    owned_errors: list[UpstreamCheckpointError] = []

    def mark(error: BaseException) -> UpstreamCheckpointError:
        marked = UpstreamCheckpointError(error)
        owned_errors.append(marked)
        return marked

    def full() -> None:
        try:
            checkpoint()
        except BaseException as error:
            raise mark(error) from None

    control = protected_git_control(checkpoint, full, mark_error=mark)
    try:
        yield control
    except UpstreamCheckpointError as error:
        if not any(error is marked for marked in owned_errors):
            raise
        raise error.error from None


def qualified_native_observer(
    checkpoint: Callable[[], None], observer: Callable[[], None] | None
) -> Callable[[], None] | None:
    """原控制创建者显式交付只读观察；不借用父局部检查或登记子 Task SQL 权限。"""
    if observer is None:
        return None
    if type(checkpoint) is not GitAuthenticationControl or not callable(observer):
        raise KernelError("git_authentication_control_invalid", "Git原生观察控制归属无效")
    origin = GitAuthenticationControl._binding(checkpoint)
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if origin[2] is not task or origin[3] != get_ident():
        raise KernelError("git_authentication_control_invalid", "Git原生观察控制归属无效")

    def observe() -> None:
        GitAuthenticationControl._binding(checkpoint, origin)
        observer()

    return observe


def native_git_checkpoint(check: Callable[[], None]) -> Callable[[], None]:
    """只在原生来源与快照边界隔离控制异常，不改写认证Reader的取消语义。"""

    def controlled() -> None:
        try:
            check()
        except UpstreamCheckpointError:
            raise
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None

    return protected_git_control(check, controlled)
