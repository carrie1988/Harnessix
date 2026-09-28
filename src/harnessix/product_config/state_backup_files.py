"""完整备份的私有文件端口：FD/原生Handle验权、有界读写和不可覆盖目录发布。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup_contracts import MAX_STATE_FILE_BYTES, MAX_STATE_FILES
from harnessix.session.maintenance_io import MaintenanceIOControl


def file_error() -> KernelError:
    return KernelError("product_backup_files_invalid", "产品备份文件权限、身份或容量无效")


def absolute_address(path: Path) -> Path:
    """只规范父目录；叶对象不得通过resolve追随链接而变为另一个授权对象。"""
    candidate = Path(os.path.abspath(path))
    if not candidate.name or str(candidate).startswith("\\\\"):
        raise file_error()
    return candidate.parent.resolve(strict=True) / candidate.name


def _parts(relative: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or str(PurePosixPath(relative)) != relative
        or any(part in {".", "..", "/"} or "\\" in part or ":" in part for part in parts)
    ):
        raise file_error()
    return parts


def _revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class PrivateStateTree:
    """只持有自有私有目录；关闭后才能发布目录，尤其不能遗留Windows共享句柄。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._resources = ExitStack()
        self._windows: Any = None
        self._windows_keys: Any = None
        self._windows_root_handle = self._root_fd = -1
        self._identity = (0, 0)
        _initialize_tree(self)

    def checkpoint(self) -> None:
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._identity:
            raise file_error()
        if os.name == "posix":
            from harnessix.product_config.session_key_posix import _private, _private_acl

            _private(info, directory=True)
            _private_acl(self._root_fd)
        elif os.name == "nt":
            self._windows.security.verify_root(self._windows_root_handle)

    def _windows_port(self, relative: str) -> Any:
        """Key目录及其文件始终走原严格端口，不能按普通状态继承形态接受。"""
        return (
            self._windows_keys
            if _parts(relative)[0].casefold() == "session-auth"
            else self._windows
        )

    def directory(self, relative: str) -> None:
        parts = _parts(relative)
        self.checkpoint()
        if os.name == "posix":
            with _posix_directory(self, parts, create=True):
                pass
        else:
            current = self.path
            for part in parts:
                current /= part
                files = self._windows_port(current.relative_to(self.path).as_posix())
                files.create_directory(current)
                handle = files.open(current, directory=True)
                files.kernel.CloseHandle(handle)

    def open_file(
        self, relative: str, *, create: bool = False, writable: bool = False
    ) -> AbstractContextManager[int]:
        """原句柄验权及关闭后复核；创建只允许自有新文件，不覆盖原对象。"""
        return _open_private_file(self, relative, create=create, writable=writable)

    def files(
        self,
        control: MaintenanceIOControl,
        *,
        directories: Callable[[str], bool] | None = None,
        transient: Callable[[str], bool] | None = None,
    ) -> tuple[str, ...]:
        """有界列举受管事实；只容忍已知生命周期文件消失，拒绝其他漂移。"""
        return _tree_files(self, control, directories=directories, transient=transient)

    def close(self) -> None:
        self._resources.close()

    def __enter__(self) -> PrivateStateTree:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_private_tree(path: Path) -> None:
    """只在已存在、受信父目录创建自有叶目录；不修复宽权限存量目录。"""
    if os.name == "posix":
        from harnessix.product_config.state_owner_posix import _trusted_parent

        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _trusted_parent(descriptor)
            os.mkdir(path.name, 0o700, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    elif os.name == "nt":
        from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
        from harnessix.workspace.windows import WindowsWorkspaceRoot
        from harnessix.workspace.windows_private_security import PrivateStateSecurity

        root = WindowsWorkspaceRoot(path.parent)
        files = WindowsKeyFiles(root, security_factory=PrivateStateSecurity)
        try:
            if os.path.lexists(path):
                raise FileExistsError
            files.create_directory(path)
        finally:
            files.close()
            root.close()
    else:
        raise file_error()


def copy_file(
    source: PrivateStateTree,
    target: PrivateStateTree,
    relative: str,
    control: MaintenanceIOControl,
) -> None:
    with source.open_file(relative) as reader, target.open_file(relative, create=True) as writer:
        before = os.fstat(reader)
        total = 0
        while chunk := os.read(reader, 1024**2):
            control.checkpoint()
            total += len(chunk)
            if total > MAX_STATE_FILE_BYTES:
                raise file_error()
            write_all(writer, chunk)
        if total != before.st_size or _revision(before) != _revision(os.fstat(reader)):
            raise file_error()
        os.fsync(writer)


def write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    while offset < len(body):
        written = os.write(descriptor, body[offset:])
        if written <= 0:
            raise file_error()
        offset += written


def file_digest(
    tree: PrivateStateTree, relative: str, control: MaintenanceIOControl
) -> tuple[int, str]:
    with tree.open_file(relative) as descriptor:
        before = os.fstat(descriptor)
        digest, total = hashlib.sha256(), 0
        while chunk := os.read(descriptor, 1024**2):
            control.checkpoint()
            total += len(chunk)
            if total > MAX_STATE_FILE_BYTES:
                raise file_error()
            digest.update(chunk)
        if total != before.st_size or _revision(before) != _revision(os.fstat(descriptor)):
            raise file_error()
        return total, digest.hexdigest()


def file_revisions(
    tree: PrivateStateTree, paths: tuple[str, ...], control: MaintenanceIOControl
) -> dict[str, tuple[int, ...]]:
    """固定原文件修改版本；仅比较路径集合不能发现复制后的原事实漂移。"""
    revisions = {}
    for path in paths:
        control.checkpoint()
        with tree.open_file(path) as descriptor:
            revisions[path] = _revision(os.fstat(descriptor))
    return revisions


def read_small(tree: PrivateStateTree, relative: str, limit: int) -> bytes:
    with tree.open_file(relative) as descriptor:
        before = os.fstat(descriptor)
        body = os.read(descriptor, limit + 1)
        if len(body) > limit or len(body) != before.st_size:
            raise file_error()
        if _revision(before) != _revision(os.fstat(descriptor)):
            raise file_error()
        return body


def write_new(tree: PrivateStateTree, relative: str, body: bytes) -> None:
    with tree.open_file(relative, create=True) as descriptor:
        write_all(descriptor, body)
        os.fsync(descriptor)


def publish_tree(source: Path, target: Path) -> None:
    """平台原生不可覆盖目录Rename；不以exists检查加普通rename替代原子排他发布。"""
    if os.name == "nt":
        from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        root = WindowsWorkspaceRoot(target.parent)
        files = WindowsKeyFiles(root)
        try:
            files.publish(source, target)
        finally:
            files.close()
            root.close()
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes, rename.restype = (
            [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint],
            ctypes.c_int,
        )
        result = rename(os.fsencode(source), os.fsencode(target), 0x4)
    else:
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result != 0:
        raise OSError(ctypes.get_errno(), "private directory publication failed")
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _posix_directory(
    tree: PrivateStateTree, parts: tuple[str, ...], *, create: bool = False
) -> Iterator[int]:
    from harnessix.product_config.session_key_posix import _private, _private_acl

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as resources:
        descriptor = os.open(tree.path, flags)
        resources.callback(os.close, descriptor)
        _private(os.fstat(descriptor), directory=True)
        _private_acl(descriptor)
        for name in parts:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(name, flags, dir_fd=descriptor)
            resources.callback(os.close, child)
            _private(os.fstat(child), directory=True)
            _private_acl(child)
            descriptor = child
        yield descriptor


@contextmanager
def _open_private_file(
    tree: PrivateStateTree, relative: str, *, create: bool = False, writable: bool = False
) -> Iterator[int]:
    parts = _parts(relative)
    tree.checkpoint()
    if create and len(parts) > 1:
        tree.directory("/".join(parts[:-1]))
    with ExitStack() as resources:
        if os.name == "posix":
            from harnessix.product_config.session_key_posix import _private, _private_acl

            parent = resources.enter_context(_posix_directory(tree, parts[:-1]))
            flags = os.O_RDWR if create or writable else os.O_RDONLY
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(
                parts[-1], flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
            )
            resources.callback(os.close, descriptor)
            _private(os.fstat(descriptor))
            _private_acl(descriptor)
        else:
            import msvcrt

            chain, _ = tree._windows.root._open_chain("/".join(parts[:-1]) or ".", data=False)
            resources.callback(tree._windows.root._close_all, chain)
            _verify_windows_parent_chain(tree, parts, chain, resources)
            files = tree._windows_port(relative)
            handle = files.open(
                tree.path / relative,
                create=1 if create else 3,
                writable=create or writable,
                exclusive=create or writable,
            )
            try:
                descriptor = int(
                    msvcrt.open_osfhandle(  # type: ignore[attr-defined]
                        handle,
                        getattr(os, "O_BINARY", 0)
                        | (os.O_RDWR if create or writable else os.O_RDONLY),
                    )
                )
            except BaseException:
                tree._windows.kernel.CloseHandle(handle)
                raise
            resources.callback(os.close, descriptor)
        yield descriptor
        tree.checkpoint()
        if os.name == "posix":
            if _revision(os.fstat(descriptor)) != _revision((tree.path / relative).lstat()):
                raise file_error()
            _private(os.fstat(descriptor))
            _private_acl(descriptor)
            os.fsync(parent) if create else None
        else:
            _verify_windows_file(tree, relative, handle)


def _verify_windows_file(tree: PrivateStateTree, relative: str, handle: int) -> None:
    """不用语义不同的fstat/lstat时间比较；以原生修订绑定叶路径与原读写Handle。"""
    files = tree._windows_port(relative)
    native = files.root

    def revision(value: Any) -> tuple[object, ...]:
        return (
            *native._revision_identity(value),
            value.creation_time.high,
            value.creation_time.low,
        )

    before = revision(native._information(handle))
    # 仅READ_CONTROL/READ_ATTRIBUTES，兼容原排他写Handle；不共享DELETE、不读取正文。
    checked = files.open(tree.path / relative, metadata_only=True)
    try:
        observed = revision(native._information(checked))
        after = revision(native._information(handle))
        if before != observed or before != after:
            raise file_error()
        files.security.verify(handle)
    finally:
        files.kernel.CloseHandle(checked)


def _verify_windows_parent_chain(
    tree: PrivateStateTree, parts: tuple[str, ...], chain: list[int], resources: ExitStack
) -> None:
    """元数据Handle不能读DACL；为受管父段另开READ_CONTROL句柄并绑定原链身份。"""
    root = tree._windows.root
    for index, directory in enumerate(chain[-len(parts) :]):
        files = tree._windows_port("/".join(parts[:index])) if index else tree._windows
        checked = files.open(tree.path.joinpath(*parts[:index]), directory=True)
        resources.callback(files.kernel.CloseHandle, checked)
        if root._object_identity(root._information(checked)) != root._object_identity(
            root._information(directory)
        ):
            raise file_error()


def _tree_files(
    tree: PrivateStateTree,
    control: MaintenanceIOControl,
    *,
    directories: Callable[[str], bool] | None = None,
    transient: Callable[[str], bool] | None = None,
) -> tuple[str, ...]:
    output: list[str] = []
    pending = [""]
    count = 0
    while pending:
        control.checkpoint()
        prefix = pending.pop()
        if prefix:
            if os.name == "posix":
                with _posix_directory(tree, _parts(prefix)):
                    pass
            else:
                files = tree._windows_port(prefix)
                handle = files.open(tree.path / prefix, directory=True)
                files.kernel.CloseHandle(handle)
        for name in sorted(os.listdir(tree.path / prefix)):
            count += 1
            if count > MAX_STATE_FILES * 2:
                raise file_error()
            relative = f"{prefix}/{name}" if prefix else name
            _parts(relative)
            try:
                info = (tree.path / relative).lstat()
            except FileNotFoundError:
                if transient is not None and transient(relative):
                    continue
                raise
            if stat.S_ISDIR(info.st_mode):
                if directories is not None and not directories(relative):
                    raise file_error()
                pending.append(relative)
            elif stat.S_ISREG(info.st_mode):
                try:
                    with tree.open_file(relative):
                        pass
                except FileNotFoundError:
                    # 已识别的SQLite生命周期文件可由最后一个只读连接合法移除。
                    # 仅容忍不存在；链接、宽权限、ACL和其他身份错误仍拒绝。
                    if transient is not None and transient(relative):
                        continue
                    raise
                output.append(relative)
            else:
                raise file_error()
    if len(output) > MAX_STATE_FILES:
        raise file_error()
    tree.checkpoint()
    return tuple(sorted(output))


def _initialize_tree(tree: PrivateStateTree) -> None:
    try:
        if os.name == "posix":
            tree._root_fd = tree._resources.enter_context(_posix_directory(tree, ()))
        elif os.name == "nt":
            from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
            from harnessix.workspace.windows import WindowsWorkspaceRoot
            from harnessix.workspace.windows_private_security import PrivateStateSecurity

            root = WindowsWorkspaceRoot(tree.path)
            tree._resources.callback(root.close)
            # SQLite保留原连接与锁时允许读写共享，但不允许DELETE共享或替换原对象。
            tree._windows = WindowsKeyFiles(
                root, security_factory=PrivateStateSecurity, shared_reads=True
            )
            tree._resources.callback(tree._windows.close)
            tree._windows_keys = WindowsKeyFiles(root)
            tree._resources.callback(tree._windows_keys.close)
            handle = tree._windows.open(tree.path, directory=True)
            tree._windows_root_handle = handle
            tree._resources.callback(tree._windows.kernel.CloseHandle, handle)
            tree._windows.security.verify_root(handle)
        else:
            raise file_error()
        info = tree.path.lstat()
        tree._identity = (info.st_dev, info.st_ino)
    except BaseException:
        tree.close()
        raise
