from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Event
from types import TracebackType
from typing import Self

from harnessix.agent.cancellation import TurnCancelled
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS, ReadToolError

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
    def __init__(self) -> None:
        self.stopped = Event()
        self.deadline = time.monotonic() + READ_TIMEOUT_SECONDS

    def checkpoint(self) -> None:
        if self.stopped.is_set():
            raise TurnCancelled
        if time.monotonic() >= self.deadline:
            raise ReadToolError("timeout")


def _parts(path: str) -> tuple[str, ...]:
    try:
        encoded = path.encode("utf-8")
    except UnicodeError:
        raise ReadToolError("path_denied") from None
    if (
        not encoded
        or len(encoded) > 1024
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ReadToolError("path_denied")
    if path == ".":
        return ()
    parts = tuple(path.split("/"))
    if len(parts) > 64 or any(p in {"", ".", ".."} or len(p.encode()) > 255 for p in parts):
        raise ReadToolError("path_denied")
    return parts


class Workspace:
    """宿主选择的本地目录能力；生命周期内保留根 FD，不等价于 OS Sandbox。"""

    def __init__(self, root: Path, *, denied_paths: tuple[str, ...] = ()) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("当前工作区实现仅支持具备 no-follow 的 POSIX 系统")
        self._denied_paths = tuple(sorted({_parts(p.casefold()) for p in denied_paths}))
        self.root = root.resolve(strict=True)
        self._root_fd: int | None = self._open_root()
        info = os.fstat(self._root_fd)
        self._identity = identity(info)
        self.scope = digest(
            {
                "policy": "workspace-read/v1",
                "root": str(self.root),
                "identity": self._identity,
                "denied_paths": self._denied_paths,
                "denied_names": sorted(_DENIED_NAMES),
                "denied_suffixes": _DENIED_SUFFIXES,
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
        parts = _parts(path)
        folded = tuple(p.casefold() for p in parts)
        if any(
            p in _DENIED_NAMES or p.startswith(".env.") or p.endswith(_DENIED_SUFFIXES)
            for p in folded
        ) or any(folded[: len(p)] == p for p in self._denied_paths):
            raise ReadToolError("path_denied")
        return parts

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
    def open(self, path: str, operation: ReadOperation, *, directory: bool) -> Iterator[int]:
        parts = self.parts(path)
        operation.checkpoint()
        with ExitStack() as stack:
            fd = self._current_root()
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
                if identity(before) != identity(after):
                    raise ReadToolError("workspace_changed")
                links.append((parent, part, fd))
            before_read = os.fstat(fd)
            self._check_type(before_read, directory=directory)
            yield fd
            operation.checkpoint()
            if revision_state(before_read) != revision_state(os.fstat(fd)):
                raise ReadToolError("workspace_changed")
            check_root = self._current_root()
            os.close(check_root)
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
