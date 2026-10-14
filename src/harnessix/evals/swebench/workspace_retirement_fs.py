"""封存专用描述符级FS：固定私有身份、独占原子发布和不越界的树清理。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from harnessix.domain.file_lock import acquire_exclusive_file_lock
from harnessix.evals.swebench.workspace_retirement_contracts import (
    RETIRED_WORKSPACE_MARKER,
    SEALED_WORKSPACE_FILE,
    WorkspaceFileIdentity,
    _invalid,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_PUBLICATION_FILES = frozenset({SEALED_WORKSPACE_FILE, RETIRED_WORKSPACE_MARKER})


@dataclass(frozen=True)
class _Scope:
    root: Path
    case_root: Path
    workspace: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.root / "cases",
            self.case_root,
            self.case_root / "run",
            self.root / "workspaces",
            self.workspace,
        )


@dataclass(frozen=True)
class _Directory:
    fd: int
    identity: WorkspaceFileIdentity


@dataclass(frozen=True)
class _FileEvidence:
    identity: WorkspaceFileIdentity
    sha256: str
    stamp: tuple[int, ...]
    body: bytes


def _identity(info: os.stat_result) -> WorkspaceFileIdentity:
    return WorkspaceFileIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        owner=info.st_uid,
        mode=stat.S_IMODE(info.st_mode),
    )


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _check_directory(info: os.stat_result, *, private: bool) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (
            stat.S_IMODE(info.st_mode) != 0o700
            if private
            else (stat.S_IMODE(info.st_mode) & 0o700 != 0o700 or info.st_mode & 0o022)
        )
    ):
        raise _invalid()


def _check_file(info: os.stat_result, *, links: int = 1) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != links
    ):
        raise _invalid()


def _open_directory(path: Path) -> _Directory:
    """逐级固定绝对路径，祖先目录的符号链接也不跟随。"""
    fd = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        _check_directory(info, private=True)
        return _Directory(fd, _identity(info))
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _opened(scope: _Scope) -> Iterator[dict[Path, _Directory]]:
    # 合同导入保持平台无关；实际退役必须具有 POSIX 描述符及拒绝跟随链接的能力。
    if os.name != "posix" or not all(
        getattr(os, name, 0) for name in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise _invalid()
    with ExitStack() as stack:
        directories: dict[Path, _Directory] = {}
        for path in scope.directories:
            try:
                directory = _open_directory(path)
            except FileNotFoundError:
                continue
            directories[path] = directory
            stack.callback(os.close, directory.fd)
            root = directories.get(scope.root)
            if root is not None and directory.identity.device != root.identity.device:
                raise _invalid()
            if path == scope.root:
                # 复用现有锁原语并锁定目录，避免新增持久锁文件。
                acquire_exclusive_file_lock(directory.fd)
        yield directories


def _assert_directories(directories: dict[Path, _Directory]) -> None:
    for path, expected in directories.items():
        parent = directories.get(path.parent)
        if parent is None:
            current = _open_directory(path)
            try:
                if current.identity != expected.identity:
                    raise _invalid()
            finally:
                os.close(current.fd)
        else:
            info = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
            _check_directory(info, private=True)
            if _identity(info) != expected.identity:
                raise _invalid()
        if _identity(os.fstat(expected.fd)) != expected.identity:
            raise _invalid()


def _present(directory: _Directory | None, name: str) -> bool:
    if directory is None:
        return False
    try:
        os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _reject_unpublished_alias(directory: _Directory | None, name: str) -> None:
    """未发布的固定暂存名不构成恢复证据，既不接管也不删除。"""
    if name not in _PUBLICATION_FILES or (
        _present(directory, f".{name}.tmp") and not _present(directory, name)
    ):
        raise _invalid()


def _check_publication_file(directory: _Directory, name: str, info: os.stat_result) -> None:
    """仅封存和标记允许两链接，且第二个必须是同目录的固定暂存别名。"""
    temporary = f".{name}.tmp"
    if name not in _PUBLICATION_FILES or not _present(directory, temporary):
        _check_file(info)
        return
    _check_file(info, links=2)
    alias = os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)
    _check_file(alias, links=2)
    if _stamp(alias) != _stamp(info):
        raise _invalid()


def _file(directory: _Directory, name: str, max_bytes: int | None) -> _FileEvidence:
    fd = os.open(name, _FILE_FLAGS, dir_fd=directory.fd)
    try:
        before = os.fstat(fd)
        _check_publication_file(directory, name, before)
        if max_bytes is not None and before.st_size > max_bytes:
            raise _invalid()
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            if size > before.st_size:
                raise _invalid()
            if max_bytes is not None:
                if size > max_bytes:
                    raise _invalid()
                chunks.append(chunk)
            hasher.update(chunk)
        if (
            size != before.st_size
            or _stamp(os.fstat(fd)) != _stamp(before)
            or _stamp(os.stat(name, dir_fd=directory.fd, follow_symlinks=False)) != _stamp(before)
        ):
            raise _invalid()
        _check_publication_file(directory, name, os.fstat(fd))
        return _FileEvidence(
            _identity(before), hasher.hexdigest(), _stamp(before), b"".join(chunks)
        )
    finally:
        os.close(fd)


def _recover_publication_alias(
    directory: _Directory, name: str, expected: _FileEvidence
) -> _FileEvidence:
    """仅由已认证退役入口调用；删除已发布同 inode 别名，不改写目标正文。"""
    if name not in _PUBLICATION_FILES:
        raise _invalid()
    info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    if _stamp(info) != expected.stamp:
        raise _invalid()
    _check_publication_file(directory, name, info)
    if info.st_nlink == 1:
        return expected
    os.unlink(f".{name}.tmp", dir_fd=directory.fd)
    os.fsync(directory.fd)
    recovered = _file(directory, name, len(expected.body))
    if recovered.identity != expected.identity or recovered.sha256 != expected.sha256:
        raise _invalid()
    return recovered


def _publish(directory: _Directory, name: str, body: bytes) -> None:
    """先fsync私有暂存文件，再以独占link原子发布，不覆盖原封存。"""
    temporary = f".{name}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    # 固定暂存名不累积分片；未知残留不是证据，不删除或截断未知inode。
    fd = os.open(temporary, flags, 0o600, dir_fd=directory.fd)
    temporary_identity = _identity(os.fstat(fd))
    try:
        os.fchmod(fd, 0o600)
        temporary_identity = _identity(os.fstat(fd))
        remaining = memoryview(body)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise _invalid()
            remaining = remaining[written:]
        os.fsync(fd)
        _check_file(os.fstat(fd))
        if _identity(os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)) != _identity(
            os.fstat(fd)
        ):
            raise _invalid()
        os.link(
            temporary,
            name,
            src_dir_fd=directory.fd,
            dst_dir_fd=directory.fd,
            follow_symlinks=False,
        )
    finally:
        try:
            if (
                _identity(os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False))
                != temporary_identity
            ):
                raise _invalid()
            os.unlink(temporary, dir_fd=directory.fd)
            os.fsync(directory.fd)
        finally:
            os.close(fd)


def _tree_entry(fd: int, name: str, device: int) -> os.stat_result:
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if info.st_uid != os.getuid() or info.st_dev != device:
        raise _invalid()
    if stat.S_ISDIR(info.st_mode):
        _check_directory(info, private=False)
    elif stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1 or info.st_mode & 0o022:
            raise _invalid()
    elif not stat.S_ISLNK(info.st_mode):
        raise _invalid()
    return info


def _walk(
    fd: int,
    device: int,
    *,
    clean: bool,
    checkpoint: Callable[[], None],
    fault: Callable[[str], None] | None,
    root: bool = True,
) -> None:
    for name in sorted(os.listdir(fd)):
        if root and name == RETIRED_WORKSPACE_MARKER:
            continue
        checkpoint()
        before = _tree_entry(fd, name, device)
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            try:
                if _identity(os.fstat(child)) != _identity(before):
                    raise _invalid()

                def child_checkpoint(name: str = name, before: os.stat_result = before) -> None:
                    checkpoint()
                    if _identity(_tree_entry(fd, name, device)) != _identity(before):
                        raise _invalid()

                _walk(
                    child,
                    device,
                    clean=clean,
                    checkpoint=child_checkpoint,
                    fault=fault,
                    root=False,
                )
            finally:
                os.close(child)
        checkpoint()
        after = _tree_entry(fd, name, device)
        if _identity(before) != _identity(after) or stat.S_IFMT(before.st_mode) != stat.S_IFMT(
            after.st_mode
        ):
            raise _invalid()
        if clean:
            if stat.S_ISDIR(before.st_mode):
                os.rmdir(name, dir_fd=fd)
            else:
                # 只删除符号链接自身，不读取或触碰外部target。
                os.unlink(name, dir_fd=fd)
            if fault is not None:
                os.fsync(fd)
                fault("during_cleanup")
    if clean:
        os.fsync(fd)
