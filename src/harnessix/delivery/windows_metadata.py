"""Windows事务元数据边界：拒绝命名流及特殊文件，替换不静默改变Owner和DACL。"""

from __future__ import annotations

import ctypes
import hashlib
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.workspace.windows import _ByHandleFileInformation


def _unsupported() -> KernelError:
    return KernelError(
        "delivery_metadata_unsupported",
        "Windows事务仅支持无附加流、非只读、无特殊属性且继承权限一致的普通文件",
    )


def check_regular_file(kernel: Any, handle: int, info: _ByHandleFileInformation) -> None:
    """禁止通过整体替换丢失ADS、EFS、压缩、隐藏等未纳入版本合同的元数据。"""

    if info.links != 1:
        raise KernelError("workspace_path_denied", "Windows事务文件具有多个硬链接")
    if info.attributes & 0x400:
        raise KernelError("workspace_path_denied", "Windows事务目标是Reparse Point")
    if info.attributes & ~0xA0 or kernel.GetFileType(handle) != 1:
        raise _unsupported()
    buffer = ctypes.create_string_buffer(16_384)
    if not kernel.GetFileInformationByHandleEx(handle, 7, buffer, len(buffer)):
        raise _unsupported()
    raw = buffer.raw
    next_offset = int.from_bytes(raw[:4], "little")
    name_bytes = int.from_bytes(raw[4:8], "little")
    if next_offset or name_bytes != len("::$DATA".encode("utf-16-le")):
        raise _unsupported()
    if raw[24 : 24 + name_bytes] != "::$DATA".encode("utf-16-le"):
        raise _unsupported()


class WindowsFileSecurity:
    """只观察权限，不提权、不修改ACL；不允许默认临时文件放宽既有文件权限。"""

    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.api = ctypes.__dict__["WinDLL"]("advapi32", use_last_error=True)
        pointer, unsigned = ctypes.c_void_p, ctypes.c_uint32
        self.api.GetSecurityInfo.argtypes = [
            pointer,
            ctypes.c_int,
            unsigned,
            pointer,
            pointer,
            pointer,
            pointer,
            ctypes.POINTER(pointer),
        ]
        self.api.GetSecurityInfo.restype = unsigned
        self.api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            pointer,
            unsigned,
            unsigned,
            ctypes.POINTER(pointer),
            ctypes.POINTER(unsigned),
        ]
        self.api.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int

    def digest(self, handle: int) -> str:
        """短期比较Owner/Group/DACL及继承控制，不持久化SID或完整安全描述符。"""

        descriptor, text = ctypes.c_void_p(), ctypes.c_void_p()
        if self.api.GetSecurityInfo(handle, 1, 7, None, None, None, None, ctypes.byref(descriptor)):
            raise _unsupported()
        try:
            size = ctypes.c_uint32()
            if (
                not self.api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    descriptor, 1, 7, ctypes.byref(text), ctypes.byref(size)
                )
                or not text.value
                or not 0 < size.value <= 65_536
            ):
                raise _unsupported()
            value = ctypes.wstring_at(text.value, size.value - 1)
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        finally:
            if text.value:
                self.kernel.LocalFree(text)
            self.kernel.LocalFree(descriptor)

    def require_equal(self, before: int, temporary: int) -> None:
        """自定义Owner/ACL不被默认为普通权限；不支持时保留原对象并明确拒绝。"""

        if self.digest(before) != self.digest(temporary):
            raise _unsupported()
