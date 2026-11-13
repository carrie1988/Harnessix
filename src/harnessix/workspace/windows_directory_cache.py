"""一次读取操作内复用已打开、已核对的目录句柄（Beta Issue #21）。

Windows上读每个文件都要从根逐段打开父目录，再打开文件本身；16000个文件的整仓搜索
约需9秒，超过5秒读取时限（ADR 0160之后在Windows真机上的测量）。这里每个目录在一次
读取操作里只打开并核对一次（不是Reparse Point、是目录、最终路径在根下），其下的文件
只打开文件这一段；文件本身的核对（不跟随Reparse Point、单一硬链接、最终路径在根下）不变。

- 目录句柄与会话根相同，共享读写、不共享删除：编辑器与构建工具仍可在这些目录里新建、
  改名文件，但这些目录本身在这次操作期间不能被改名、删除或换成Junction，按路径打开
  其下的文件仍解析到核对过的目录。
- 根与POSIX的固定根（ADR 0160）相同，开头与结尾按路径各核对一次身份；根之上的某一级
  在操作期间被替换，结尾报workspace_changed，这次结果作废。
- 目录数达到上限，或打开目录、文件时遇到意外的系统错误，返回None，由调用方走完整路径。
- 缓存只属于一次操作，结束时全部关闭。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from harnessix.agent.errors import KernelError
from harnessix.workspace.paths import normalize_workspace_path
from harnessix.workspace.windows import (
    _FILE_ATTRIBUTE_DIRECTORY,
    _FILE_ATTRIBUTE_REPARSE_POINT,
    WindowsWorkspaceRoot,
    _observe_opened,
    _Observed,
)

MAX_CACHED_DIRECTORIES: Final = 8192


class DirectoryHandles:
    """一次读取操作内已核对的目录句柄，按工作区内的相对路径保存。"""

    def __init__(self, root: WindowsWorkspaceRoot) -> None:
        self._root = root
        self._handles: dict[str, int] = {}
        self._root_checked = False

    def observe(
        self,
        path: str,
        *,
        include_content: bool,
        max_bytes: int,
        checkpoint: Callable[[], None] | None,
    ) -> _Observed | None:
        """能复用目录句柄时返回观察结果；返回None时由调用方走完整路径。"""

        normalized = normalize_workspace_path(path, "windows")
        if normalized == "." or not self._check_root():
            return None
        parent = normalized.rpartition("/")[0]
        if parent and not self._directory(parent, checkpoint):
            return None
        root = self._root
        try:
            handle = root._open(_absolute(root, normalized), data=include_content)
        except OSError:
            return None
        try:
            if root._information(handle).attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise KernelError("workspace_path_denied", "Windows路径包含Reparse Point或Junction")
            return _observe_opened(
                root,
                [handle],
                root._final_path(handle),
                normalized,
                include_content=include_content,
                max_bytes=max_bytes,
                checkpoint=checkpoint,
            )
        except OSError:
            raise KernelError("workspace_observation_failed", "Windows资源观察失败") from None
        finally:
            root._close_all([handle])

    def close(self) -> None:
        handles, self._handles = list(self._handles.values()), {}
        self._root._close_all(handles)

    def verify_root(self) -> None:
        """结尾按路径再核对一次根；用过缓存才需要。"""

        if self._root_checked and not _root_reachable(self._root):
            raise KernelError("workspace_changed", "Windows Workspace根已经变化")

    def _check_root(self) -> bool:
        """第一次使用前按路径核对根的身份与最终路径（与完整路径的核对相同）。"""

        self._root_checked = self._root_checked or _root_reachable(self._root)
        return self._root_checked

    def _directory(self, normalized: str, checkpoint: Callable[[], None] | None) -> bool:
        if normalized in self._handles:
            return True
        parent = normalized.rpartition("/")[0]
        if parent and not self._directory(parent, checkpoint):
            return False
        if len(self._handles) >= MAX_CACHED_DIRECTORIES:
            return False
        if checkpoint is not None:
            checkpoint()
        handle = _open_verified_directory(self._root, normalized)
        if handle is None:
            return False
        self._handles[normalized] = handle
        return True


def _root_reachable(root: WindowsWorkspaceRoot) -> bool:
    """按路径打开根并核对身份与最终路径；根已变化时报workspace_changed，打不开返回False。"""

    try:
        handles, _ = root._open_chain(".", data=False)
    except OSError:
        return False
    root._close_all(handles)
    return True


def _open_verified_directory(root: WindowsWorkspaceRoot, normalized: str) -> int | None:
    """打开一个目录并核对：不是Reparse Point、是目录、最终路径在根下。系统错误返回None。"""

    try:
        handle = root._open(_absolute(root, normalized), data=False, directory=True)
    except OSError:
        return None
    try:
        info = root._information(handle)
        if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise KernelError("workspace_path_denied", "Windows路径包含Reparse Point或Junction")
        if info.attributes & _FILE_ATTRIBUTE_DIRECTORY == 0:
            raise KernelError("workspace_wrong_file_type", "Windows路径父段不是目录")
        root._assert_under_root(root._final_path(handle))
    except OSError:
        root._close_all([handle])
        return None
    except BaseException:
        root._close_all([handle])
        raise
    return handle


def _absolute(root: WindowsWorkspaceRoot, normalized: str) -> Path:
    return root.path / Path(*normalized.split("/"))
