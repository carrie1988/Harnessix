"""POSIX 原生观察的目录枚举与父操作检查组合。"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.tools.workspace import ReadOperation


class UpstreamCheckpointError(BaseException):
    """跨越原生 IO 错误转换边界，携带未经改写的上游控制异常。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error


class NativeReadOperation(ReadOperation):
    """沿用局部读取时限，并在每个检查点先消费同一父操作。"""

    def __init__(self, checkpoint: Callable[[], None]) -> None:
        super().__init__()
        self._upstream_checkpoint = checkpoint

    def checkpoint(self) -> None:
        """父取消或超时原样传出；局部取消和超时仍由原读取操作判定。"""
        try:
            self._upstream_checkpoint()
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None
        super().checkpoint()


def observe_directory(
    descriptor: int,
    *,
    max_entries: int,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[
    bytes,
    int,
    tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...],
]:
    """按原排序及对象身份编码目录，不跟随成员链接，逐项消费可选检查。"""
    entries: list[tuple[str, int, tuple[int, int]]] = []
    exposed: list[tuple[str, Literal["file", "directory", "symlink", "special"]]] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if checkpoint is not None:
                checkpoint()
            if len(entries) >= max_entries:
                raise KernelError("workspace_snapshot_limit", "Workspace目录观察超过条目上限")
            child = entry.stat(follow_symlinks=False)
            entries.append((entry.name, stat.S_IFMT(child.st_mode), (child.st_dev, child.st_ino)))
            if stat.S_ISLNK(child.st_mode):
                kind: Literal["file", "directory", "symlink", "special"] = "symlink"
            elif stat.S_ISREG(child.st_mode):
                kind = "file"
            elif stat.S_ISDIR(child.st_mode):
                kind = "directory"
            else:
                kind = "special"
            exposed.append((entry.name, kind))
    entries.sort()
    exposed.sort()
    body = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
    return body, len(entries), tuple(exposed)
