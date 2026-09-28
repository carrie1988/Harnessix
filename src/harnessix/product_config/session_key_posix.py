"""POSIX私有目录FD中的密钥发布；单链文件、不可覆盖提交和原候选恢复。"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.product_config.session_key_codec import (
    MAX_KEY_FILE_BYTES,
    POSIX_MAGIC,
    create_payload,
    decode_payload,
    unavailable,
)

_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW if os.name == "posix" else 0


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    """目录安全身份不绑定条目、大小或时间；权限与ACL仍独立复核。"""

    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _private(info: os.stat_result, *, directory: bool = False, links: int = 1) -> None:
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != links)
    ):
        raise unavailable()


def _private_acl(descriptor: int) -> None:
    """Darwin扩展ACL独立于mode bits；只接受无扩展ACL的规范私有对象。"""
    if sys.platform != "darwin":
        return
    library = ctypes.CDLL(None, use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(descriptor, 0x100)
    if acl:
        try:
            raise unavailable()
        finally:
            library.acl_free(acl)
    if ctypes.get_errno() not in {errno.ENOENT, errno.ENOATTR}:
        raise unavailable()


def _open_file(parent: int, name: str, *, create: bool = False) -> int:
    flags = os.O_RDWR | _FLAGS | (os.O_CREAT if create else 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        _private(opened)
        _private_acl(descriptor)
        if _identity(opened) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
            raise unavailable()
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(parent: int, name: str) -> bytes:
    descriptor = _open_file(parent, name)
    try:
        before = os.fstat(descriptor)
        body = os.read(descriptor, MAX_KEY_FILE_BYTES + 1)
        if (
            len(body) > MAX_KEY_FILE_BYTES
            or len(body) != before.st_size
            or _identity(before) != _identity(os.fstat(descriptor))
            or _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
        ):
            raise unavailable()
        if not body.startswith(POSIX_MAGIC):
            raise unavailable()
        decode_payload(body[len(POSIX_MAGIC) :]).close()
        return body
    finally:
        os.close(descriptor)


def _exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _settle_published(parent: int) -> None:
    if not _exists(parent, "key.v1.pending"):
        return
    key = os.stat("key.v1", dir_fd=parent, follow_symlinks=False)
    pending = os.stat("key.v1.pending", dir_fd=parent, follow_symlinks=False)
    _private(key, links=2)
    _private(pending, links=2)
    if _identity(key) != _identity(pending):
        raise unavailable()
    os.unlink("key.v1.pending", dir_fd=parent)
    os.fsync(parent)


def _publish(root: int, parent: int, fault: Callable[[str], None]) -> None:
    if any(_exists(root, name) for name in ("sessions.db", "sessions.db-wal", "sessions.db-shm")):
        raise unavailable()
    if _exists(parent, "key.v1.pending"):
        _read(parent, "key.v1.pending")
    else:
        body = POSIX_MAGIC + create_payload()
        descriptor = os.open(
            "key.v1.pending", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS, 0o600, dir_fd=parent
        )
        try:
            _private(os.fstat(descriptor))
            _private_acl(descriptor)
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise unavailable()
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    fault("key.after_pending")
    # 硬链接发布不覆盖任何既有Key；退出留下同inode双名字可在锁内结算。
    os.link("key.v1.pending", "key.v1", src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
    os.fsync(parent)
    fault("key.after_publish")
    _settle_published(parent)


def _load_locked(root: int, parent: int, fault: Callable[[str], None]) -> bytes:
    descriptor = _open_file(parent, ".lock", create=True)
    try:
        acquire_exclusive_file_lock(descriptor)
        if _exists(parent, "key.v1"):
            _settle_published(parent)
        else:
            _publish(root, parent, fault)
        body = _read(parent, "key.v1")
        fault("key.before_return")
        return body[len(POSIX_MAGIC) :]
    finally:
        os.close(descriptor)


def load_posix_key(path: Path, fault: Callable[[str], None]) -> bytes:
    """全部操作锚定私有目录FD；危险权限不静默chmod修复。"""
    root = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _FLAGS)
    parent: int | None = None
    try:
        root_info = os.fstat(root)
        _private(root_info, directory=True)
        _private_acl(root)
        if _directory_identity(root_info) != _directory_identity(path.lstat()):
            raise unavailable()
        if not _exists(root, "session-auth"):
            os.mkdir("session-auth", 0o700, dir_fd=root)
            os.fsync(root)
        parent = os.open("session-auth", os.O_RDONLY | os.O_DIRECTORY | _FLAGS, dir_fd=root)
        parent_info = os.fstat(parent)
        _private(parent_info, directory=True)
        _private_acl(parent)
        body = _load_locked(root, parent, fault)
        # 已读取Key仍不能越过期间发生的目录权限或ACL变化。
        _private(os.fstat(root), directory=True)
        _private_acl(root)
        _private(os.fstat(parent), directory=True)
        _private_acl(parent)
        current = os.stat("session-auth", dir_fd=root, follow_symlinks=False)
        if _directory_identity(parent_info) != _directory_identity(current) or (
            _directory_identity(root_info) != _directory_identity(path.lstat())
        ):
            raise unavailable()
        return body
    except BlockingIOError:
        raise KernelError("publication_key_busy", "Session密钥初始化正在进行") from None
    except OSError:
        raise unavailable() from None
    finally:
        if parent is not None:
            os.close(parent)
        os.close(root)
