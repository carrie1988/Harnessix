"""Workspace身份与租约：使用Windows句柄链拒绝Reparse Point与路径逃逸。"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import ResourceAccess
from harnessix.workspace.paths import normalize_workspace_path
from harnessix.workspace.snapshot import (
    MAX_SNAPSHOT_DIRECTORY_ENTRIES,
    MAX_SNAPSHOT_FILE_BYTES,
)

_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FILE_READ_DATA = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_READONLY = 0x00000001
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3


def _last_error() -> int:
    return int(ctypes.__dict__["get_last_error"]())


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation_time", _FileTime),
        ("access_time", _FileTime),
        ("write_time", _FileTime),
        ("volume_serial", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


class _Observed:
    def __init__(
        self,
        kind: Literal["file", "directory", "missing"],
        identity: tuple[object, ...],
        content: bytes | None,
        size: int,
        entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...]
        | None = None,
    ) -> None:
        self.kind = kind
        self.identity = identity
        self.content = content
        self.size = size
        self.entries = entries


class WindowsWorkspaceRoot:
    """Windows句柄链端口；逐段拒绝Reparse Point并禁止检查后重命名。"""

    def __init__(self, path: Path) -> None:
        if os.name != "nt":
            raise KernelError("workspace_platform_unsupported", "Windows端口只能在Windows运行")
        self._kernel32 = ctypes.__dict__["WinDLL"]("kernel32", use_last_error=True)
        self._configure_api()
        self.path = Path(os.path.abspath(path))
        self._root_handle: int | None = None
        try:
            handles, final_path = self._open_chain(".")
        except (OSError, KernelError):
            raise KernelError("workspace_binding_invalid", "Windows Workspace根绑定失败") from None
        try:
            info = self._information(handles[-1])
            if info.attributes & _FILE_ATTRIBUTE_DIRECTORY == 0:
                raise KernelError("workspace_binding_invalid", "Windows Workspace根不是目录")
            self.root_identity = self._object_identity(info)
            self._final_root = self._final_path(handles[-1]).casefold().rstrip("\\")
            if final_path.casefold().rstrip("\\") != self._final_root:
                raise KernelError("workspace_binding_invalid", "Windows Workspace根身份不一致")
            self._root_handle = handles.pop()
        except BaseException:
            self._close_all(handles)
            raise
        self._close_all(handles)

    def _configure_api(self) -> None:
        pointer = ctypes.c_void_p
        self._kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            pointer,
            ctypes.c_uint32,
            ctypes.c_uint32,
            pointer,
        ]
        self._kernel32.CreateFileW.restype = pointer
        self._kernel32.GetFileInformationByHandle.argtypes = [
            pointer,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = ctypes.c_int
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            pointer,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
        self._kernel32.ReadFile.argtypes = [
            pointer,
            pointer,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            pointer,
        ]
        self._kernel32.ReadFile.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [pointer]
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def _open(self, path: Path, *, data: bool) -> int:
        access = _FILE_READ_ATTRIBUTES | (_FILE_READ_DATA if data else 0)
        handle = self._kernel32.CreateFileW(
            self._api_path(path),
            access,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE or handle is None:
            error = _last_error()
            raise OSError(error, os.strerror(error), str(path))
        return int(handle)

    def _open_chain(
        self,
        logical_path: str,
        *,
        data: bool = True,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[list[int], str]:
        normalized = normalize_workspace_path(logical_path, "windows")
        current = Path(self.path.anchor)
        paths = [current]
        for part in self.path.parts[1:]:
            current /= part
            paths.append(current)
        root_index = len(paths) - 1
        if normalized != ".":
            for part in normalized.split("/"):
                current /= part
                paths.append(current)
        handles: list[int] = []
        try:
            for index, candidate in enumerate(paths):
                if checkpoint is not None:
                    checkpoint()
                handle = self._open(
                    candidate,
                    data=data and index == len(paths) - 1,
                )
                handles.append(handle)
                info = self._information(handle)
                if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise KernelError(
                        "workspace_path_denied", "Windows路径包含Reparse Point或Junction"
                    )
                if index < len(paths) - 1 and info.attributes & _FILE_ATTRIBUTE_DIRECTORY == 0:
                    raise KernelError("workspace_path_denied", "Windows路径父段不是目录")
                if self._root_handle is not None and index == root_index:
                    if (
                        self._object_identity(info) != self.root_identity
                        or self._final_path(handle).casefold().rstrip("\\") != self._final_root
                    ):
                        raise KernelError("workspace_changed", "Windows Workspace根已经变化")
            final = self._final_path(handles[-1])
            return handles, final
        except BaseException:
            self._close_all(handles)
            raise

    def observe(
        self,
        path: str,
        *,
        access: ResourceAccess,
        include_content: bool = True,
        max_bytes: int = MAX_SNAPSHOT_FILE_BYTES,
        checkpoint: Callable[[], None] | None = None,
    ) -> _Observed:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError("Windows读取上限无效")
        normalized = normalize_workspace_path(path, "windows")
        try:
            handles, final_path = self._open_chain(
                normalized,
                data=include_content,
                checkpoint=checkpoint,
            )
        except OSError as error:
            if (
                error.errno not in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}
                or normalized == "."
            ):
                raise KernelError("workspace_observation_failed", "Windows资源观察失败") from None
            parent, _, name = normalized.rpartition("/")
            try:
                handles, final_path = self._open_chain(
                    parent or ".",
                    checkpoint=checkpoint,
                )
            except OSError:
                raise KernelError(
                    "workspace_parent_missing", "Windows缺失资源的父目录不存在"
                ) from None
            try:
                self._assert_under_root(final_path)
                parent_info = self._information(handles[-1])
                return _Observed(
                    "missing",
                    (*self._stable_identity(parent_info, directory=True), name or normalized),
                    None,
                    0,
                )
            finally:
                self._close_all(handles)
        try:
            self._assert_under_root(final_path)
            handle = handles[-1]
            before = self._information(handle)
            directory = bool(before.attributes & _FILE_ATTRIBUTE_DIRECTORY)
            if not directory and before.links != 1:
                raise KernelError("workspace_path_denied", "Windows文件具有多个硬链接")
            if directory:
                body, count, entries = self._directory_body(
                    self.path / Path(*normalized.split("/")),
                    checkpoint=checkpoint,
                )
                after = self._information(handle)
                if self._revision_identity(before) != self._revision_identity(after):
                    raise KernelError("workspace_changed", "Windows目录在观察期间变化")
                return _Observed(
                    "directory",
                    (
                        *self._stable_identity(before, directory=True),
                        hashlib.sha256(body).hexdigest(),
                    ),
                    body,
                    count,
                    entries,
                )
            size = (before.size_high << 32) | before.size_low
            if not include_content:
                after = self._information(handle)
                if self._revision_identity(before) != self._revision_identity(after):
                    raise KernelError("workspace_changed", "Windows文件在观察期间变化")
                return _Observed(
                    "file",
                    self._stable_identity(before, directory=False),
                    None,
                    size,
                )
            if size > max_bytes:
                raise KernelError("workspace_snapshot_limit", "Windows文件超过快照上限")
            content = self._read_all(handle, max_bytes=max_bytes, checkpoint=checkpoint)
            after = self._information(handle)
            if (
                self._revision_identity(before) != self._revision_identity(after)
                or len(content) != size
            ):
                raise KernelError("workspace_changed", "Windows文件在观察期间变化")
            return _Observed("file", self._stable_identity(before, directory=False), content, size)
        except KernelError:
            raise
        except OSError:
            raise KernelError("workspace_observation_failed", "Windows资源观察失败") from None
        finally:
            self._close_all(handles)

    def _directory_body(
        self,
        path: Path,
        *,
        checkpoint: Callable[[], None] | None,
    ) -> tuple[
        bytes,
        int,
        tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...],
    ]:
        entries: list[tuple[str, int, int, int]] = []
        exposed: list[tuple[str, Literal["file", "directory", "symlink", "special"]]] = []
        seen: set[str] = set()
        with os.scandir(self._api_path(path)) as iterator:
            for entry in iterator:
                if checkpoint is not None:
                    checkpoint()
                if len(entries) >= MAX_SNAPSHOT_DIRECTORY_ENTRIES:
                    raise KernelError("workspace_snapshot_limit", "Windows目录观察超过条目上限")
                # Windows DirEntry.stat()对普通项可能复用FindFirstFileW缓存，显式os.stat
                # 才能取得Python 3.12提供的当前File Index（st_ino）。
                info = os.stat(entry.path, follow_symlinks=False)
                attributes = int(getattr(info, "st_file_attributes", 0))
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    kind = _FILE_ATTRIBUTE_REPARSE_POINT
                    exposed_kind: Literal["file", "directory", "symlink", "special"] = "symlink"
                elif stat.S_ISDIR(info.st_mode):
                    kind = _FILE_ATTRIBUTE_DIRECTORY
                    exposed_kind = "directory"
                else:
                    kind = 0
                    exposed_kind = "file" if stat.S_ISREG(info.st_mode) else "special"
                folded = entry.name.casefold()
                if folded in seen:
                    raise KernelError("workspace_path_denied", "Windows目录包含大小写折叠冲突")
                seen.add(folded)
                # FindFirstFileW返回的时间和大小可能来自目录枚举缓存。目录资源只绑定
                # 成员名称、类型和对象身份；被显式选择的文件另由句柄身份与内容摘要绑定。
                entries.append((folded, kind, info.st_dev, info.st_ino))
                exposed.append((entry.name, exposed_kind))
        entries.sort()
        exposed.sort(key=lambda item: item[0].casefold())
        return (
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode(),
            len(entries),
            tuple(exposed),
        )

    def _read_all(
        self,
        handle: int,
        *,
        max_bytes: int,
        checkpoint: Callable[[], None] | None,
    ) -> bytes:
        body = bytearray()
        while True:
            if checkpoint is not None:
                checkpoint()
            remaining = max_bytes - len(body)
            buffer = ctypes.create_string_buffer(min(65536, remaining + 1))
            read = ctypes.c_uint32()
            if not self._kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                error = _last_error()
                raise OSError(error, os.strerror(error))
            if read.value == 0:
                return bytes(body)
            body.extend(buffer.raw[: read.value])
            if len(body) > max_bytes:
                raise KernelError("workspace_snapshot_limit", "Windows文件超过快照上限")

    def _information(self, handle: int) -> _ByHandleFileInformation:
        info = _ByHandleFileInformation()
        if not self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            error = _last_error()
            raise OSError(error, os.strerror(error))
        return info

    @staticmethod
    def _object_identity(info: _ByHandleFileInformation) -> tuple[object, ...]:
        return (info.volume_serial, info.file_index_high, info.file_index_low)

    @classmethod
    def _revision_identity(cls, info: _ByHandleFileInformation) -> tuple[object, ...]:
        return (
            *cls._object_identity(info),
            info.attributes,
            info.links,
            info.size_high,
            info.size_low,
            info.write_time.high,
            info.write_time.low,
        )

    @classmethod
    def _stable_identity(
        cls, info: _ByHandleFileInformation, *, directory: bool
    ) -> tuple[object, ...]:
        """返回可跨观察比较的语义身份，不持久化Windows易变时间元数据。"""

        identity: tuple[object, ...] = (
            *cls._object_identity(info),
            info.attributes & _FILE_ATTRIBUTE_READONLY,
            info.links,
        )
        if directory:
            return identity
        return (*identity, info.size_high, info.size_low)

    def _final_path(self, handle: int) -> str:
        size = self._kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            error = _last_error()
            raise OSError(error, os.strerror(error))
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = self._kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise OSError("GetFinalPathNameByHandleW返回无效长度")
        return buffer.value

    def _assert_under_root(self, final_path: str) -> None:
        candidate = final_path.casefold().rstrip("\\")
        if candidate != self._final_root and not candidate.startswith(self._final_root + "\\"):
            raise KernelError("workspace_path_denied", "Windows最终对象位于Workspace外")

    def _close_all(self, handles: list[int]) -> None:
        for handle in reversed(handles):
            self._kernel32.CloseHandle(handle)

    def close(self) -> None:
        if self._root_handle is not None:
            self._kernel32.CloseHandle(self._root_handle)
            self._root_handle = None

    @staticmethod
    def _api_path(path: Path) -> str:
        value = os.path.abspath(path)
        if value.startswith("\\\\?\\"):
            return value
        if value.startswith("\\\\"):
            return "\\\\?\\UNC\\" + value[2:]
        return "\\\\?\\" + value


def windows_port_source_digest() -> str:
    """提供稳定实现标识，供能力证据绑定，不包含宿主路径。"""

    return hashlib.sha256(b"harnessix-windows-workspace/v1").hexdigest()
