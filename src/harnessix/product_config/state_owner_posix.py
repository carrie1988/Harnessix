"""POSIX全状态锁端口：复用私有Key文件校验，根外目录FD绑定不随Root替换漂移。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from harnessix.product_config.session_key_posix import (
    _directory_identity,
    _identity,
    _open_file,
    _private,
    _private_acl,
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _trusted_parent(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise OSError
    _private_acl(descriptor)


@contextmanager
def open_state_owner(state_root: Path, anchor: Path) -> Iterator[tuple[int, Callable[[], None]]]:
    """保持父目录、私有锚点和单链锁的原FD；ACL或地址漂移固定拒绝。"""
    with ExitStack() as resources:
        parent = os.open(state_root.parent, _DIRECTORY_FLAGS)
        resources.callback(os.close, parent)
        _trusted_parent(parent)
        parent_identity = _directory_identity(os.fstat(parent))
        try:
            os.mkdir(anchor.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        directory = os.open(anchor.name, _DIRECTORY_FLAGS, dir_fd=parent)
        resources.callback(os.close, directory)
        _private(os.fstat(directory), directory=True)
        _private_acl(directory)
        directory_identity = _directory_identity(os.fstat(directory))
        descriptor = _open_file(directory, ".lock", create=True)
        resources.callback(os.close, descriptor)
        os.fsync(directory)

        def checkpoint() -> None:
            _trusted_parent(parent)
            _private(os.fstat(directory), directory=True)
            _private_acl(directory)
            _private(os.fstat(descriptor))
            _private_acl(descriptor)
            if (
                parent_identity != _directory_identity(state_root.parent.lstat())
                or directory_identity
                != _directory_identity(os.stat(anchor.name, dir_fd=parent, follow_symlinks=False))
                or _identity(os.fstat(descriptor))
                != _identity(os.stat(".lock", dir_fd=directory, follow_symlinks=False))
            ):
                raise OSError

        checkpoint()
        yield descriptor, checkpoint
