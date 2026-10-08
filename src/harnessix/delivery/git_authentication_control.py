"""Git 纯计算段的有界控制契约；完整来源认证仍在段边界和原 I/O 端口执行。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import get_ident
from types import MethodType
from typing import cast

from harnessix.agent.errors import KernelError

_Origin = tuple[Callable[[], None], Callable[[], None], asyncio.Task[object] | None, int]
_ORIGIN_FIELDS = ("_local_check", "_authenticate", "_task", "_thread")


def _current_task() -> asyncio.Task[object] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _validated_origin(
    control: GitAuthenticationControl,
    origin: _Origin,
    segment: object | None,
    expected: _Origin | None,
) -> _Origin:
    """只校验声明字段与创建绑定；不认证环境或执行字典／键的魔术方法。"""
    current = object.__getattribute__(control, "__dict__")
    if type(current) is not dict:
        raise KernelError("git_authentication_control_invalid", "Git控制创建绑定已改变")
    fields = tuple(current.items())
    if any(type(name) is not str for name, _ in fields):
        raise KernelError("git_authentication_control_invalid", "Git控制创建绑定已改变")
    # 稀疏字典的 copy 可能重算碰撞键；仅从已验证的原生字符串键重建。
    current = dict(fields)
    if (
        type(origin) is not tuple
        or len(origin) != len(_ORIGIN_FIELDS)
        or current.get("_origin", origin) is not origin
        or current.get("_segment", segment) is not segment
        or (expected is not None and origin is not expected)
        or any(
            current.get(name) is not value
            for name, value in zip(_ORIGIN_FIELDS, origin, strict=True)
        )
    ):
        raise KernelError("git_authentication_control_invalid", "Git控制创建绑定已改变")
    return cast(_Origin, origin)


class GitAuthenticationControl:
    """分开本地频检与完整认证，不缓存认证结果或签发持久能力。

    仅宿主原操作创建；普通调用仍执行完整检查。只有显式纯计算段收到局部
    检查点，段外、嵌套撤销、另一 Task 或线程使用旧检查点都回到完整检查。
    """

    contract_version = "harnessix.git-authentication-control/v2"
    __slots__ = ("__origin", "__segment", "__dict__")

    def __init__(self, local_check: Callable[[], None], authenticate: Callable[[], None]) -> None:
        self._local_check = local_check
        self._authenticate = authenticate
        self._task, self._thread = _current_task(), get_ident()
        self.__origin = (local_check, authenticate, self._task, self._thread)
        self.__segment: object | None = None

    @property
    def _origin(self) -> _Origin:
        """创建期绑定不放在可替换 __dict__ 中，也不提供重新绑定入口。"""
        return self.__origin

    @_origin.setter
    def _origin(self, value: _Origin) -> None:
        raise KernelError("git_authentication_control_invalid", "Git控制创建绑定已改变")

    @property
    def _segment(self) -> object | None:
        """token 只读观察；声明字典不能改变、恢复或删除实际段状态。"""
        return self.__segment

    @_segment.setter
    def _segment(self, value: object | None) -> None:
        raise KernelError("git_authentication_control_invalid", "Git控制创建绑定已改变")

    def _binding(self, expected: _Origin | None = None) -> _Origin:
        """当前字段不是新的创建证明；先拒绝可执行字典／键，再比对原绑定。"""
        return _validated_origin(self, self.__origin, self.__segment, expected)

    def __call__(self) -> None:
        """完整检查先撤销纯段，防止上游回调借用保存的局部检查点。"""
        self.__segment = None
        origin = GitAuthenticationControl._binding(self)
        origin[1]()
        GitAuthenticationControl._binding(self, origin)

    @contextmanager
    def pure(self) -> Iterator[Callable[[], None]]:
        """同步无 I/O 的纯段；异常先撤销，不用退出认证遮盖原首失败。"""
        GitAuthenticationControl.__call__(self)
        origin = GitAuthenticationControl._binding(self)
        local_check, _, task, thread = origin
        if (
            type(self) is not GitAuthenticationControl
            or _current_task() is not task
            or get_ident() != thread
        ):
            # 受管子 Task 可走原完整观察，但不能继承父 Task 的纯段频检。
            # 使用完整委托而非控制实例，避免严格编解码入口再次递归适配。
            yield MethodType(GitAuthenticationControl.__call__, self)
            GitAuthenticationControl.__call__(self)
            return
        segment = object()
        self.__segment = segment

        def check() -> None:
            if (
                type(self) is GitAuthenticationControl
                and self.__segment is segment
                and _current_task() is task
                and get_ident() == thread
            ):
                GitAuthenticationControl._binding(self, origin)
                local_check()
            else:
                GitAuthenticationControl.__call__(self)

        try:
            yield check
            check()
        finally:
            if self.__segment is segment:
                self.__segment = None
        GitAuthenticationControl.__call__(self)


@contextmanager
def pure_git_authentication(checkpoint: Callable[[], None]) -> Iterator[Callable[[], None]]:
    """只接受原控制实例分层；任意函数、代理或子类保持其原完整调用轨迹。"""
    if type(checkpoint) is GitAuthenticationControl:
        with GitAuthenticationControl.pure(checkpoint) as check:
            yield check
    else:
        yield checkpoint
