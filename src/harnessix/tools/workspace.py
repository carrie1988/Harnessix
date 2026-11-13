"""内置Coding Tool：把Workspace只读能力注册为版本化Tool。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Event
from types import TracebackType
from typing import TYPE_CHECKING, Self

from harnessix.agent.cancellation import TurnCancelled
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS, ReadToolError

if TYPE_CHECKING:
    from harnessix.workspace.windows_directory_cache import DirectoryHandles

_DENIED_NAMES = frozenset(
    {
        ".git",
        ".harnessix",
        ".codex",
        ".ssh",
        ".aws",
        ".gnupg",
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def revision_state(info: os.stat_result) -> tuple[int, ...]:
    return (
        *identity(info),
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class ReadOperation:
    def __init__(self, *, timeout_seconds: int | float = READ_TIMEOUT_SECONDS) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("读取操作时限必须是有限正数")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("读取操作时限必须是有限正数")
        self.stopped = Event()
        self.deadline = time.monotonic() + timeout
        # Workspace.pinned_root在本次操作期间固定的根目录描述符；为None时每次打开都按路径核对根。
        self.pinned_root: int | None = None
        # Windows：本次操作内已核对的目录句柄（windows_read_port.directory_scope）；
        # 为None时每个文件都从根走完整的句柄链。
        self.windows_directories: DirectoryHandles | None = None

    def checkpoint(self) -> None:
        if self.stopped.is_set():
            raise TurnCancelled
        if time.monotonic() >= self.deadline:
            raise ReadToolError("timeout")


async def run_read_operation[T](
    reader: Callable[[ReadOperation], T],
    *,
    timeout_seconds: int | float = READ_TIMEOUT_SECONDS,
) -> T:
    """在线程中执行受限读取；调用方取消时先通知并回收线程。"""

    operation = ReadOperation(timeout_seconds=timeout_seconds)
    worker = asyncio.create_task(asyncio.to_thread(reader, operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        operation.stopped.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def _parts(path: str, *, max_bytes: int = 1024, max_parts: int = 64) -> tuple[str, ...]:
    try:
        encoded = path.encode("utf-8")
    except UnicodeError:
        raise ReadToolError("path_denied") from None
    if (
        not encoded
        or len(encoded) > max_bytes
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ReadToolError("path_denied")
    if path == ".":
        return ()
    parts = tuple(path.split("/"))
    if len(parts) > max_parts or any(p in {"", ".", ".."} or len(p.encode()) > 255 for p in parts):
        raise ReadToolError("path_denied")
    return parts


class WorkspaceReadPolicy:
    """跨平台共享的逻辑路径与敏感名称拒绝策略。"""

    def __init__(
        self,
        denied_paths: tuple[str, ...] = (),
        *,
        path_max_bytes: int = 1024,
        path_max_parts: int = 64,
    ) -> None:
        self.denied_paths = tuple(sorted({_parts(path.casefold()) for path in denied_paths}))
        self.path_max_bytes = path_max_bytes
        self.path_max_parts = path_max_parts

    def parts(self, path: str) -> tuple[str, ...]:
        parts = _parts(
            path,
            max_bytes=self.path_max_bytes,
            max_parts=self.path_max_parts,
        )
        folded = tuple(part.casefold() for part in parts)
        if any(
            part in _DENIED_NAMES or part.startswith(".env.") or part.endswith(_DENIED_SUFFIXES)
            for part in folded
        ) or any(folded[: len(prefix)] == prefix for prefix in self.denied_paths):
            raise ReadToolError("path_denied")
        return parts

    def scope_fields(self) -> dict[str, object]:
        return {
            "denied_paths": self.denied_paths,
            "denied_names": sorted(_DENIED_NAMES),
            "denied_suffixes": _DENIED_SUFFIXES,
        }


class Workspace:
    """宿主选择的本地目录能力；生命周期内保留根 FD，不等价于 OS Sandbox。"""

    def __init__(
        self,
        root: Path,
        *,
        denied_paths: tuple[str, ...] = (),
        path_max_bytes: int = 1024,
        path_max_parts: int = 64,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("当前工作区实现仅支持具备 no-follow 的 POSIX 系统")
        self._policy = WorkspaceReadPolicy(
            denied_paths,
            path_max_bytes=path_max_bytes,
            path_max_parts=path_max_parts,
        )
        self.root = root.resolve(strict=True)
        self._root_fd: int | None = self._open_root()
        info = os.fstat(self._root_fd)
        self._identity = identity(info)
        self.scope = digest(
            {
                "policy": "workspace-read/v1",
                "root": str(self.root),
                "identity": self._identity,
                **self._policy.scope_fields(),
            }
        )

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open("/", flags)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def parts(self, path: str) -> tuple[str, ...]:
        return self._policy.parts(path)

    def _current_root(self) -> int:
        if self._root_fd is None:
            raise ReadToolError("workspace_changed")
        try:
            fd = self._open_root()
        except OSError:
            raise ReadToolError("workspace_changed") from None
        if identity(os.fstat(fd)) != self._identity:
            os.close(fd)
            raise ReadToolError("workspace_changed")
        return fd

    @contextmanager
    def open(
        self,
        path: str,
        operation: ReadOperation,
        *,
        directory: bool,
        same_device: int | None = None,
    ) -> Iterator[int]:
        parts = self.parts(path)
        operation.checkpoint()
        with ExitStack() as stack:
            fd = _root(self, operation)
            stack.callback(os.close, fd)
            links: list[tuple[int, str, int]] = []
            for index, part in enumerate(parts):
                operation.checkpoint()
                parent = fd
                expected_directory = index < len(parts) - 1 or directory
                before = os.stat(part, dir_fd=parent, follow_symlinks=False)
                self._check_type(before, directory=expected_directory)
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                if expected_directory:
                    flags |= os.O_DIRECTORY
                try:
                    fd = os.open(part, flags, dir_fd=parent)
                except PermissionError:
                    raise
                except OSError:
                    raise ReadToolError("workspace_changed") from None
                stack.callback(os.close, fd)
                after = os.fstat(fd)
                self._check_type(after, directory=expected_directory)
                if same_device is not None and after.st_dev != same_device:
                    raise ReadToolError("path_denied")
                if identity(before) != identity(after):
                    raise ReadToolError("workspace_changed")
                links.append((parent, part, fd))
            before_read = os.fstat(fd)
            self._check_type(before_read, directory=directory)
            yield fd
            operation.checkpoint()
            if revision_state(before_read) != revision_state(os.fstat(fd)):
                raise ReadToolError("workspace_changed")
            os.close(_root(self, operation))
            try:
                for parent, name, child in links:
                    operation.checkpoint()
                    if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(
                        os.fstat(child)
                    ):
                        raise ReadToolError("workspace_changed")
            except OSError:
                raise ReadToolError("workspace_changed") from None

    @staticmethod
    def _check_type(info: os.stat_result, *, directory: bool) -> None:
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ReadToolError("path_denied")
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ReadToolError("wrong_file_type")

    def close(self) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _root(workspace: Workspace, operation: ReadOperation) -> int:
    """本次打开的起点：操作固定了根时用它的副本，否则按路径重新打开并核对根。"""
    pinned = getattr(operation, "pinned_root", None)
    return workspace._current_root() if pinned is None else os.dup(pinned)


@contextmanager
def pinned_root(workspace: Workspace, operation: ReadOperation) -> Iterator[None]:
    """一次读取操作只在开头与结尾按路径核对根，其间各次打开都从这个根出发（Beta Issue #21）。

    此前每打开一个文件，前后都要从“/”逐段重新打开并核对根，16000个文件的仓库5秒内搜不完。
    固定的描述符始终指向原来的根目录：其间根路径即使被替换，读取也不会落到别处，结束时的核对报
    workspace_changed。各路径段的no-follow与身份核对不变。固定只属于这一次操作，不在Workspace上共享。
    """
    if operation.pinned_root is not None:
        yield
        return
    operation.pinned_root = workspace._current_root()
    try:
        yield
    finally:
        os.close(operation.pinned_root)
        operation.pinned_root = None
    os.close(workspace._current_root())
