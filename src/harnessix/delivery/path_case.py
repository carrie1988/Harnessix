"""POSIX Workspace路径在文件系统实际大小写语义下的别名识别（ADR 0068第2版）。

POSIX路径按字节比较，但macOS默认APFS等文件系统不区分大小写：已有src/a.txt时，src/A.txt指向同一个
文件；同一批里的a.txt与A.txt也是同一个文件。按字节规划会把别名当成另一个路径，发布到第二项时才发现
文件已变，留下部分效果。规划前在这里识别别名并整批拒绝：已有路径以磁盘上的名称为准，同批两项若在
该目录的语义下是同一名字也拒绝。只读取目录项与元数据，不写入工作区。
"""

from __future__ import annotations

import os
import stat
import sys
import unicodedata
from collections.abc import Iterable
from contextlib import ExitStack
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import Workspace, identity

CASE_CONFLICT = "delivery_path_case_conflict"
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def reject_case_aliases(root: str | Path, paths: Iterable[str]) -> None:
    """路径须与磁盘上的名称逐段一致，且同批路径在各自目录的大小写语义下互不相同。"""
    with Workspace(Path(root), path_max_bytes=4096, path_max_parts=128) as workspace:
        resolver = _Resolver(workspace)
        claimed: dict[tuple[tuple[int, int], str], str] = {}
        for path in paths:
            try:
                directory, leaf = resolver.leaf_key(path)
            except (ReadToolError, OSError):
                # 受拒路径、权限或类型问题交给规划器原有检查报告，不在这里改写错误。
                continue
            if directory is None:
                continue
            key = (directory, leaf)
            if key in claimed:
                raise KernelError(
                    CASE_CONFLICT,
                    f"同一批修改中{claimed[key]}与{path}在本工作区指向同一个文件"
                    "（文件系统不区分大小写）；请把它们合并为一项",
                )
            claimed[key] = path


class _Resolver:
    """逐段打开目录（不跟随链接），缓存目录项与该目录的大小写语义。"""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._entries: dict[tuple[int, int], frozenset[str]] = {}
        self._folds: dict[tuple[int, int], bool] = {}

    def leaf_key(self, path: str) -> tuple[tuple[int, int] | None, str]:
        """返回末段所在目录身份与比较名；路径中有不存在的父目录时交给规划器原有检查。"""
        with ExitStack() as stack:
            return self._walk(path, stack)

    def _walk(self, path: str, stack: ExitStack) -> tuple[tuple[int, int] | None, str]:
        parts = self._workspace.parts(path)
        descriptor = self._workspace._current_root()
        stack.callback(os.close, descriptor)
        parent: tuple[int, str] | None = None
        for index, name in enumerate(parts):
            entries = self._listing(descriptor)
            if name not in entries:
                real = self._alias(descriptor, entries, name)
                if real is not None:
                    actual = "/".join((*parts[:index], real, *parts[index + 1 :]))
                    raise KernelError(
                        CASE_CONFLICT,
                        f"路径{path}与磁盘上的已有路径{actual}只差大小写或字符写法；本工作区的文件系统"
                        f"不区分大小写，请改用磁盘上的名称{actual}",
                    )
            if index == len(parts) - 1:
                directory = identity(os.fstat(descriptor))
                return directory, _fold(name) if self._folds_case(descriptor, parent) else name
            if name not in entries:
                return None, name
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError:
                return None, name
            stack.callback(os.close, child)
            parent = (descriptor, name)
            descriptor = child
        return None, ""

    def _listing(self, descriptor: int) -> frozenset[str]:
        key = identity(os.fstat(descriptor))
        if key not in self._entries:
            self._entries[key] = frozenset(os.listdir(descriptor))
        return self._entries[key]

    @staticmethod
    def _alias(descriptor: int, entries: frozenset[str], name: str) -> str | None:
        """名字不在目录项中却能打开，说明文件系统把它解析成了另一个已有名字。"""
        try:
            target = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return None
        for entry in sorted(entries):
            try:
                info = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            if identity(info) == identity(target):
                return entry
        return name

    def _folds_case(self, descriptor: int, parent: tuple[int, str] | None) -> bool:
        """用目录中已有名字的大小写变体探测语义；无可用名字时用目录自身在父目录中的名字。"""
        key = identity(os.fstat(descriptor))
        if key not in self._folds:
            entries = self._listing(descriptor)
            folds = _probe(descriptor, entries)
            if folds is None and parent is not None:
                folds = _probe(parent[0], frozenset({parent[1]}))
            # 根目录为空且无法探测时按平台默认：macOS默认卷不区分大小写，其余POSIX区分。
            self._folds[key] = sys.platform == "darwin" if folds is None else folds
        return self._folds[key]


def _probe(descriptor: int, entries: frozenset[str]) -> bool | None:
    for entry in sorted(entries):
        variant = entry.swapcase()
        if variant == entry or variant in entries:
            continue
        try:
            original = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            continue
        try:
            other = os.stat(variant, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            continue
        return identity(other) == identity(original) and stat.S_IFMT(other.st_mode) == stat.S_IFMT(
            original.st_mode
        )
    return None


def _fold(name: str) -> str:
    """不区分大小写的卷通常也不区分Unicode规范形式（如APFS）；比较名同时折叠两者。"""
    return unicodedata.normalize("NFC", name).casefold()
