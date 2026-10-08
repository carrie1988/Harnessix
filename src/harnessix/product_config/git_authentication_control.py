"""Git 纯计算段的有界控制契约；完整来源认证仍在段边界和原 I/O 端口执行。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import get_ident

from harnessix.product_config.git_prepared_link_connection import _current_task


class GitAuthenticationControl:
    """分开本地频检与完整认证，不缓存认证结果或签发持久能力。

    仅宿主原操作创建；普通调用仍执行完整检查。只有显式纯计算段收到局部
    检查点，段外、嵌套撤销、另一 Task 或线程使用旧检查点都回到完整检查。
    """

    contract_version = "harnessix.git-authentication-control/v2"

    def __init__(self, local_check: Callable[[], None], authenticate: Callable[[], None]) -> None:
        self._local_check = local_check
        self._authenticate = authenticate
        self._task, self._thread = _current_task(), get_ident()
        self._segment: object | None = None

    def __call__(self) -> None:
        """完整检查先撤销纯段，防止上游回调借用保存的局部检查点。"""
        self._segment = None
        self._authenticate()

    @contextmanager
    def pure(self) -> Iterator[Callable[[], None]]:
        """同步无 I/O 的纯段；异常先撤销，不用退出认证遮盖原首失败。"""
        self()
        if _current_task() is not self._task or get_ident() != self._thread:
            # 受管子 Task 可走原完整观察，但不能继承父 Task 的纯段频检。
            # 使用完整委托而非控制实例，避免严格编解码入口再次递归适配。
            yield self.__call__
            self()
            return
        segment = object()
        self._segment = segment

        def check() -> None:
            if (
                self._segment is segment
                and _current_task() is self._task
                and get_ident() == self._thread
            ):
                self._local_check()
            else:
                self()

        try:
            yield check
            check()
        finally:
            if self._segment is segment:
                self._segment = None
        self()


@contextmanager
def pure_git_authentication(checkpoint: Callable[[], None]) -> Iterator[Callable[[], None]]:
    """只接受原控制实例分层；任意函数、代理或子类保持其原完整调用轨迹。"""
    if type(checkpoint) is GitAuthenticationControl:
        with checkpoint.pure() as check:
            yield check
    else:
        yield checkpoint
