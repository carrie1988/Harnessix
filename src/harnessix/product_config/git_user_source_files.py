"""Git 固定原生来源的同步只读复核；不提供写锁、ABA 连续性或提交原子性。"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_user_observation_paths import (
    _physical_path,
    invalid_git_user_paths,
)
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import WorkspaceReadPolicy, _parts
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError
from harnessix.workspace.snapshot import (
    MAX_SNAPSHOT_DIRECTORY_ENTRIES,
    MAX_SNAPSHOT_TOTAL_BYTES,
    _digest,
    _native_platform,
    _NativeObservation,
    _NativeRoot,
    _open_native,
    _PosixRoot,
)

if TYPE_CHECKING:
    from harnessix.workspace.windows import WindowsWorkspaceRoot

MAX_SOURCE_NODES = MAX_SNAPSHOT_DIRECTORY_ENTRIES
MAX_SOURCE_BYTES = MAX_SNAPSHOT_TOTAL_BYTES
_FILES = ("HEAD", "config", "config.worktree", "commondir", "packed-refs")
_TREES = ("refs", "reftable")


def _changed() -> KernelError:
    return KernelError("git_user_observation_changed", "Git用户观察期间来源发生变化")


def _limit() -> KernelError:
    return KernelError("git_user_source_limit", "Git用户原生来源超过观察上限")


class _SourceReadPolicy(WorkspaceReadPolicy):
    """共享工具策略禁止 .git；私有根仅开放固定来源，保留原生路径校验。"""

    def parts(self, path: str) -> tuple[str, ...]:
        parts = _parts(path, max_bytes=4096, max_parts=128)
        if not parts or (len(parts) == 1 and parts[0] in (".git", *_FILES)) or parts[0] in _TREES:
            return parts
        raise ReadToolError("path_denied")


class _Checkpoint:
    """控制异常穿越原生转换边界；始终保留第一次异常实例。"""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.error: BaseException | None = None

    def __call__(self) -> None:
        try:
            if self.error is not None:
                raise self.error
            self.callback()
        except BaseException as error:
            if self.error is None:
                self.error = error
            raise UpstreamCheckpointError(self.error) from None


@contextmanager
def _native_errors(control: _Checkpoint, *, verifying: bool) -> Iterator[None]:
    try:
        yield
    except BaseException as error:
        if control.error is not None:
            raise control.error from None
        if isinstance(error, KernelError):
            if error.code == "workspace_snapshot_limit":
                raise _limit() from None
            if error.code in {"git_user_source_limit", "git_user_observation_changed"}:
                raise
            if verifying or error.code in {
                "workspace_changed",
                "workspace_workspace_changed",
                "workspace_parent_missing",
            }:
                raise _changed() from None
            raise invalid_git_user_paths() from None
        if isinstance(error, (OSError, ReadToolError, ValueError, RuntimeError)):
            raise (_changed() if verifying else invalid_git_user_paths()) from None
        raise


@dataclass(frozen=True, slots=True, repr=False)
class _SourceObject:
    kind: str
    identity: str
    sha256: str | None = None
    size: int = 0
    entries: tuple[tuple[str, str], ...] | None = None


def _source_object(observed: _NativeObservation) -> _SourceObject:
    if observed.kind == "file" and (
        observed.content is None or len(observed.content) != observed.size
    ):
        raise _changed()
    if observed.kind == "directory" and observed.entries is None:
        raise invalid_git_user_paths()
    return _SourceObject(
        observed.kind,
        _digest(observed.identity),
        hashlib.sha256(observed.content).hexdigest() if observed.content is not None else None,
        observed.size,
        observed.entries,
    )


def _directory_identity(native: _NativeRoot, path: str, control: _Checkpoint) -> tuple[object, ...]:
    """沿原句柄链只读目录对象身份，不枚举根或 locator 的其他成员。"""
    if isinstance(native, _PosixRoot):
        with native._workspace.open(
            path,
            NativeReadOperation(control),
            directory=True,
            same_device=native._root_device,
        ) as descriptor:
            posix_info = os.fstat(descriptor)
            return posix_info.st_dev, posix_info.st_ino
    windows = cast("WindowsWorkspaceRoot", native)
    from harnessix.workspace.windows import _FILE_ATTRIBUTE_DIRECTORY

    handles, final_path = windows._open_chain(path, data=False, checkpoint=control)
    try:
        windows._assert_under_root(final_path)
        windows_info = windows._information(handles[-1])
        if not windows_info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise invalid_git_user_paths()
        return windows._object_identity(windows_info)
    finally:
        windows._close_all(handles)


def _locator(native: _NativeRoot, control: _Checkpoint) -> _SourceObject:
    """固定 .git：普通文件读取完整正文，目录只绑定身份，不读取 gitdir 或 Index。"""
    if isinstance(native, _PosixRoot):
        operation = NativeReadOperation(control)
        with native._workspace.open(".", operation, directory=True) as parent:
            try:
                posix_info = os.stat(".git", dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return _SourceObject("missing", _digest((*native.root_identity, ".git")))
            if stat.S_ISDIR(posix_info.st_mode):
                identity = _directory_identity(native, ".git", control)
                if identity != (posix_info.st_dev, posix_info.st_ino):
                    raise _changed()
                return _SourceObject("directory", _digest(identity))
            if not stat.S_ISREG(posix_info.st_mode):
                raise invalid_git_user_paths()
            observed = native._observe_existing(".git", operation, directory=False, access="read")
            if observed.identity[:2] != (posix_info.st_dev, posix_info.st_ino):
                raise _changed()
            return _source_object(observed)
    from harnessix.workspace.windows import (
        _FILE_ATTRIBUTE_DIRECTORY,
        _observe_file,
        _observe_missing,
    )

    windows = cast("WindowsWorkspaceRoot", native)
    try:
        handles, final_path = windows._open_chain(".git", checkpoint=control)
    except OSError as error:
        if error.errno not in {2, 3}:
            raise
        return _source_object(_observe_missing(windows, ".git", control))
    try:
        windows._assert_under_root(final_path)
        windows_info = windows._information(handles[-1])
        if windows_info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            return _SourceObject("directory", _digest(windows._object_identity(windows_info)))
        if windows_info.links != 1:
            raise invalid_git_user_paths()
        from harnessix.workspace.snapshot import MAX_SNAPSHOT_FILE_BYTES

        return _source_object(
            _observe_file(
                windows,
                handles[-1],
                windows_info,
                include_content=True,
                max_bytes=MAX_SNAPSHOT_FILE_BYTES,
                checkpoint=control,
            )
        )
    finally:
        windows._close_all(handles)


@dataclass(slots=True)
class GitUserSourceFiles:
    """本次实际来源的私有摘要集合；只允许同步复核，不导出配置正文。"""

    _root: _NativeRoot = field(repr=False)
    _sources: tuple[_NativeRoot, ...] = field(repr=False)
    _roots: tuple[_NativeRoot, ...] = field(repr=False)
    _baseline: dict[tuple[int, str], _SourceObject] = field(default_factory=dict, repr=False)
    _closed: bool = field(default=False, repr=False)

    def _verify_roots(self, control: _Checkpoint) -> None:
        with _native_errors(control, verifying=True):
            control()
            if self._closed:
                raise _changed()
            for native in self._roots:
                _physical_path(native.path)
                if _directory_identity(native, ".", control) != native.root_identity:
                    raise _changed()
            control()

    def _capture(self, control: _Checkpoint) -> dict[tuple[int, str], _SourceObject]:
        self._verify_roots(control)
        nodes = len(self._roots) + 1 + len(self._sources) * (len(_FILES) + len(_TREES))
        if nodes > MAX_SOURCE_NODES:
            raise _limit()
        locator = _locator(self._root, control)
        result = {(id(self._root), ".git"): locator}
        total_bytes = locator.size if locator.kind == "file" else 0
        if total_bytes > MAX_SOURCE_BYTES:
            raise _limit()
        for native in self._sources:
            # 离开目录时再次观察成员；迭代栈避免递归深度改变原端口边界。
            pending: list[tuple[str, str, _SourceObject | None]] = [
                (path, "tree" if path in _TREES else "file", None)
                for path in reversed((*_FILES, *_TREES))
            ]
            while pending:
                path, expected, previous = pending.pop()
                control()
                with _native_errors(
                    control, verifying=previous is not None or expected in {"directory", "regular"}
                ):
                    observed = _source_object(
                        native.observe(path, access="read", checkpoint=control)
                    )
                if previous is not None:
                    if observed != previous:
                        raise _changed()
                    continue
                if expected == "file" and observed.kind not in {"file", "missing"}:
                    raise invalid_git_user_paths()
                if expected == "tree" and observed.kind not in {"directory", "missing"}:
                    raise invalid_git_user_paths()
                if expected in {"directory", "regular"} and observed.kind != (
                    "file" if expected == "regular" else "directory"
                ):
                    raise _changed()
                result[(id(native), path)] = observed
                if observed.kind == "file":
                    total_bytes += observed.size
                    if total_bytes > MAX_SOURCE_BYTES:
                        raise _limit()
                elif observed.kind == "directory":
                    with _native_errors(control, verifying=True):
                        verified = _source_object(
                            native.observe(path, access="read", checkpoint=control)
                        )
                    if verified != observed:
                        raise _changed()
                    entries = observed.entries or ()
                    nodes += len(entries)
                    if nodes > MAX_SOURCE_NODES:
                        raise _limit()
                    if any(kind not in {"file", "directory"} for _, kind in entries):
                        raise invalid_git_user_paths()
                    pending.append((path, expected, observed))
                    pending.extend(
                        (f"{path}/{name}", "regular" if kind == "file" else "directory", None)
                        for name, kind in reversed(entries)
                    )
        with _native_errors(control, verifying=True):
            if _locator(self._root, control) != locator:
                raise _changed()
        self._verify_roots(control)
        return result

    def verify(self, checkpoint: Callable[[], None]) -> None:
        """重新读取逐对象身份、完整文件摘要、目录成员和缺失状态，比较冻结基线。"""
        control = _Checkpoint(checkpoint)
        with _native_errors(control, verifying=True):
            if self._capture(control) != self._baseline:
                raise _changed()


def _close_roots(roots: list[_NativeRoot], *, suppress: bool) -> None:
    failed = False
    for native in reversed(roots):
        try:
            native.close()
        except BaseException:
            failed = True
    if failed and not suppress:
        raise _changed() from None


@contextmanager
def pin_git_user_source_files(
    root: Path, common: Path, admin: Path, *, checkpoint: Callable[[], None]
) -> Iterator[GitUserSourceFiles]:
    """进入即捕获并复核；退出只回收本次句柄，不用 finally 复核遮盖正文异常。"""
    roots: list[_NativeRoot] = []
    pinned: GitUserSourceFiles | None = None
    control = _Checkpoint(checkpoint)
    try:
        with _native_errors(control, verifying=False):
            control()
            platform = _native_platform()
            by_path: dict[Path, _NativeRoot] = {}
            for path in (root, common, admin):
                if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                    raise invalid_git_user_paths()
                _physical_path(path)
                if path not in by_path:
                    native = _open_native(path, platform)
                    roots.append(native)
                    if native.path != path:
                        raise invalid_git_user_paths()
                    if isinstance(native, _PosixRoot):
                        native._workspace._policy = _SourceReadPolicy()
                    by_path[path] = native
            sources = tuple(by_path[path] for path in dict.fromkeys((common, admin)))
            pinned = GitUserSourceFiles(by_path[root], sources, tuple(roots))
            pinned._baseline = pinned._capture(control)
            with _native_errors(control, verifying=True):
                if pinned._capture(control) != pinned._baseline:
                    raise _changed()
        yield pinned
    finally:
        if pinned is not None:
            pinned._closed = True
        _close_roots(roots, suppress=sys.exc_info()[0] is not None)
