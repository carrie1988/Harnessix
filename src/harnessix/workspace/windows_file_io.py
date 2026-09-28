"""Windows受管文件的共享原生IO：Win32写入/Flush与NT同目录句柄Rename。"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.workspace.windows import _INVALID_HANDLE, _api_path, _last_error

_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_READ_DATA = 0x1
_READ_ATTRIBUTES = 0x80
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_OPEN_REPARSE_POINT = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_FILE_RENAME_INFORMATION_EX = 65
_FILE_DISPOSITION_INFO_EX = 21
_REPLACE_IF_EXISTS = 0x1
_POSIX_SEMANTICS = 0x2


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("root", ctypes.c_void_p),
        ("name_bytes", ctypes.c_uint32),
        ("name", ctypes.c_uint16 * 1),
    ]


class _IoStatus(ctypes.Union):
    _fields_ = [("status", ctypes.c_int32), ("pointer", ctypes.c_void_p)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("result", _IoStatus), ("information", ctypes.c_size_t)]


def _rename_buffer(parent: int, name: str, *, replace: bool) -> ctypes.Array[ctypes.c_char]:
    """按NT ABI编码UTF-16结构及尾部空间；名称字节长度不含终止字符。"""

    body = name.encode("utf-16-le")
    offset = _RenameInfo.name.offset
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_RenameInfo) + len(body) + 2)
    header = _RenameInfo.from_buffer(buffer)
    header.flags = (_REPLACE_IF_EXISTS | _POSIX_SEMANTICS) if replace else 0
    header.root = parent
    header.name_bytes = len(body)
    ctypes.memmove(ctypes.addressof(buffer) + offset, body, len(body))
    return buffer


def _native_rename_api() -> Any:
    """只配置有安全访问检查的用户态NT Rename；不使用BypassAccessCheck信息类。"""

    native = ctypes.__dict__["WinDLL"]("ntdll", use_last_error=True)
    native.NtSetInformationFile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    native.NtSetInformationFile.restype = ctypes.c_int32
    native.RtlNtStatusToDosError.argtypes = [ctypes.c_int32]
    native.RtlNtStatusToDosError.restype = ctypes.c_uint32
    return native


def _rename_same_directory(native: Any, handle: int, name: str, *, replace: bool) -> None:
    """只允许同目录叶名称；返回值与IO完成状态都必须确认，未决效果不降级重试。"""

    if not name or name in {".", ".."} or any(char in name for char in "/\\:\x00"):
        raise ValueError("Windows同目录Rename只接受叶名称")
    buffer = _rename_buffer(0, name, replace=replace)
    completion = _IoStatusBlock()
    completion.result.status = 0x103
    status = int(
        native.NtSetInformationFile(
            handle, ctypes.byref(completion), buffer, len(buffer), _FILE_RENAME_INFORMATION_EX
        )
    )
    # 同步文件句柄必须确认完成；PENDING或完成状态失败都交给原事务观察结算。
    failure = status or completion.result.status
    if failure:
        error = int(native.RtlNtStatusToDosError(failure))
        raise OSError(error, "Windows同目录句柄Rename失败")


def _raise_io_error() -> None:
    error = _last_error()
    raise OSError(error, "Windows文件事务IO失败")


def _configure(kernel: Any) -> None:
    pointer, unsigned, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        "WriteFile": ([pointer, pointer, unsigned, ctypes.POINTER(unsigned), pointer], boolean),
        "FlushFileBuffers": ([pointer], boolean),
        "SetFilePointerEx": ([pointer, ctypes.c_int64, pointer, unsigned], boolean),
        "SetFileInformationByHandle": ([pointer, boolean, pointer, unsigned], boolean),
        "GetFileInformationByHandleEx": ([pointer, boolean, pointer, unsigned], boolean),
        "GetFileType": ([pointer], unsigned),
        "GetDriveTypeW": ([ctypes.c_wchar_p], unsigned),
        "GetVolumeInformationByHandleW": (
            [
                pointer,
                ctypes.c_wchar_p,
                unsigned,
                pointer,
                pointer,
                pointer,
                ctypes.c_wchar_p,
                unsigned,
            ],
            boolean,
        ),
        "LocalFree": ([pointer], pointer),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result


class WindowsFileOperations:
    """仅作用于调用方已经固定的Workspace父目录，不降级到路径replace/unlink。"""

    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        _configure(kernel)
        self._native = _native_rename_api()

    def require_local_ntfs(self, parent: int, anchor: str) -> None:
        """首发写端口仅接受本地固定NTFS卷；共享盘和其他文件系统失败关闭。"""

        if self.kernel.GetDriveTypeW(anchor) != 3:
            raise KernelError("delivery_platform_unsupported", "Windows事务要求本地固定NTFS卷")
        filesystem = ctypes.create_unicode_buffer(64)
        if not self.kernel.GetVolumeInformationByHandleW(
            parent, None, 0, None, None, None, filesystem, len(filesystem)
        ):
            _raise_io_error()
        if filesystem.value.upper() != "NTFS":
            raise KernelError("delivery_platform_unsupported", "Windows事务要求本地固定NTFS卷")

    def open_existing(
        self, path: Path, *, delete: bool = False, replace: bool = False
    ) -> int | None:
        """禁止新增写句柄；替换时仅共享Delete以允许内核移除旧名称，不创建父目录。"""

        access = _READ_DATA | _READ_ATTRIBUTES | _READ_CONTROL | (_DELETE if delete else 0)
        handle = self.kernel.CreateFileW(
            _api_path(path),
            access,
            1 | (4 if replace else 0),
            None,
            _OPEN_EXISTING,
            _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS,
            None,
        )
        if handle == _INVALID_HANDLE or handle is None:
            if _last_error() == 2:
                return None
            _raise_io_error()
        return int(handle)

    def create_temporary(self, path: Path) -> int:
        """排他创建同父目录临时文件；持有句柄期间不允许其他写入或改名。"""

        handle = self.kernel.CreateFileW(
            _api_path(path),
            _GENERIC_READ | _GENERIC_WRITE | _DELETE | _READ_CONTROL,
            1,
            None,
            _CREATE_NEW,
            _OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE or handle is None:
            _raise_io_error()
        return int(handle)

    def write_and_flush(self, handle: int, body: bytes) -> None:
        """处理部分写入并Flush内容；不声称文件Flush等价于硬件掉电原子性。"""

        offset = 0
        while offset < len(body):
            chunk = body[offset : offset + 65_536]
            buffer = ctypes.create_string_buffer(chunk)
            written = ctypes.c_uint32()
            if not self.kernel.WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None):
                _raise_io_error()
            if not 0 < written.value <= len(chunk):
                raise OSError("Windows事务写入长度无效")
            offset += written.value
        if not self.kernel.FlushFileBuffers(handle):
            _raise_io_error()

    def rewind(self, handle: int) -> None:
        """二次内容观察从同一句柄的文件起点开始。"""

        if not self.kernel.SetFilePointerEx(handle, 0, None, 0):
            _raise_io_error()

    def rename(self, handle: int, name: str, *, replace: bool) -> None:
        """同目录Rename由临时句柄定位父对象；调用方须保持原父链与before固定。"""

        _rename_same_directory(self._native, handle, name, replace=replace)

    def delete(self, handle: int) -> None:
        """按既有句柄标记POSIX式删除；从不绕过只读属性或删除路径上的后继对象。"""

        flags = ctypes.c_uint32(1 | _POSIX_SEMANTICS)
        if not self.kernel.SetFileInformationByHandle(
            handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)
        ):
            _raise_io_error()
