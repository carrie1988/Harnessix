"""受管副本的 FD 定位与落盘原语；不提供任意目录写能力。"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from harnessix.agent.errors import KernelError
from harnessix.patches.planner import _read_image
from harnessix.tools.workspace import ReadOperation, Workspace, digest, identity, revision_state

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def fail(code: str) -> KernelError:
    return KernelError(f"patch_{code}", "受管 Patch 操作未完成，请按错误码核对状态")


def private(info: os.stat_result, *, directory: bool) -> None:
    Workspace._check_type(info, directory=directory)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise fail("private_path_required")


def create_file(parent: int, name: str) -> int:
    return os.open(name, FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)


def write_all(fd: int, body: bytes, operation: ReadOperation) -> None:
    offset = 0
    while offset < len(body):
        operation.checkpoint()
        written = os.write(fd, body[offset : offset + 65536])
        if written <= 0:
            raise fail("short_write")
        offset += written


def snapshot(workspace: Workspace, path: str, operation: ReadOperation) -> tuple[bytes, str, int]:
    with workspace.open(path, operation, directory=False) as fd:
        revision = digest((workspace.scope, path, revision_state(os.fstat(fd))))
    return _read_image(workspace, path, operation, revision)


def plain_metadata(fd: int) -> None:
    """拒绝用户扩展属性；Darwin 私有新文件允许系统 provenance 标记。"""
    if sys.platform not in {"darwin", "linux"}:
        raise fail("unsupported_platform")
    libc = ctypes.CDLL(None, use_errno=True)
    list_attributes = libc.flistxattr
    list_attributes.restype = ctypes.c_ssize_t
    if sys.platform == "darwin":
        list_attributes.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        size = list_attributes(fd, None, 0, 0)
    else:
        list_attributes.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        size = list_attributes(fd, None, 0)
    if size < 0:
        raise OSError(ctypes.get_errno(), "扩展属性检查失败")
    if size > 4096 or getattr(os.fstat(fd), "st_flags", 0):
        raise fail("unsupported_metadata")
    if size:
        if sys.platform != "darwin":
            raise fail("unsupported_metadata")
        names = ctypes.create_string_buffer(size)
        count = list_attributes(fd, names, size, 0)
        if count < 0:
            raise OSError(ctypes.get_errno(), "扩展属性检查失败")
        if names.raw[:count] != b"com.apple.provenance\0":
            raise fail("unsupported_metadata")
    if sys.platform == "darwin":
        get_acl = libc.acl_get_fd_np
        get_acl.argtypes, get_acl.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_void_p
        acl = get_acl(fd, 0x100)  # SDK sys/acl.h: ACL_TYPE_EXTENDED
        if acl:
            free_acl = libc.acl_free
            free_acl.argtypes, free_acl.restype = [ctypes.c_void_p], ctypes.c_int
            free_acl(acl)
            raise fail("unsupported_metadata")
        if ctypes.get_errno() != errno.ENOENT:
            raise OSError(ctypes.get_errno(), "ACL 检查失败")


def writable_target(workspace: Workspace, path: str, operation: ReadOperation) -> tuple[int, int]:
    with workspace.open(path, operation, directory=False) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & ~0o777:
            raise fail("unsupported_metadata")
        plain_metadata(fd)
        return identity(info)


class WriteParent:
    def __init__(self, workspace: Workspace, fd: int, links: list[tuple[int, str, int]]) -> None:
        self.workspace = workspace
        self.fd = fd
        self.links = links

    def verify(self) -> None:
        os.close(self.workspace._current_root())
        for parent, name, child in self.links:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            private(info, directory=True)
            plain_metadata(child)
            if identity(info) != identity(os.fstat(child)):
                raise fail("workspace_changed")


@contextmanager
def write_parent(workspace: Workspace, path: str) -> Iterator[WriteParent]:
    # 不复用 Workspace.open(directory=True)：替换子文件会合法改变目录 mtime。
    with ExitStack() as stack:
        fd = workspace._current_root()
        stack.callback(os.close, fd)
        private(os.fstat(fd), directory=True)
        plain_metadata(fd)
        links: list[tuple[int, str, int]] = []
        for name in workspace.parts(path)[:-1]:
            parent = fd
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            private(before, directory=True)
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            stack.callback(os.close, fd)
            if identity(before) != identity(os.fstat(fd)):
                raise fail("workspace_changed")
            links.append((parent, name, fd))
        result = WriteParent(workspace, fd, links)
        result.verify()
        yield result
        result.verify()


def import_file(workspace: Workspace, path: str, body: bytes, mode: int, op: ReadOperation) -> None:
    with ExitStack() as stack:
        fd = workspace._current_root()
        stack.callback(os.close, fd)
        parts = workspace.parts(path)
        for name in parts[:-1]:
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            stack.callback(os.close, child)
            private(os.fstat(child), directory=True)
            plain_metadata(child)
            fd = child
        target = create_file(fd, parts[-1])
        stack.callback(os.close, target)
        write_all(target, body, op)
        os.fchmod(target, mode)
        os.fsync(target)
        os.fsync(fd)
