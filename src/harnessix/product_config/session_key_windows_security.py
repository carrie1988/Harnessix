"""Windows密钥文件原生Owner与受保护DACL；仅允许当前用户及SYSTEM。"""

from __future__ import annotations

import ctypes
from typing import Any

from harnessix.product_config.session_key_codec import unavailable


class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("descriptor", ctypes.c_void_p),
        ("inherit", ctypes.c_int),
    ]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("used", ctypes.c_uint32), ("free", ctypes.c_uint32)]


class _AllowedAce(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_ubyte),
        ("flags", ctypes.c_ubyte),
        ("size", ctypes.c_uint16),
        ("mask", ctypes.c_uint32),
        ("sid", ctypes.c_uint32),
    ]


def _configure(api: Any, kernel: Any) -> None:
    p, u, b = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        "OpenProcessToken": ([p, u, ctypes.POINTER(p)], b),
        "GetTokenInformation": ([p, b, p, u, ctypes.POINTER(u)], b),
        "ConvertSidToStringSidW": ([p, ctypes.POINTER(p)], b),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            [ctypes.c_wchar_p, u, ctypes.POINTER(p), ctypes.POINTER(u)],
            b,
        ),
        "GetSecurityInfo": (
            [p, b, u, ctypes.POINTER(p), p, ctypes.POINTER(p), p, ctypes.POINTER(p)],
            u,
        ),
        "GetSecurityDescriptorControl": (
            [p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(u)],
            b,
        ),
        "GetAclInformation": ([p, p, u, b], b),
        "GetAce": ([p, u, ctypes.POINTER(p)], b),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], p
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [p], b
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [p], p


class PrivateKeySecurity:
    """拥有短期安全描述符；读取验证从不修改既有Owner、ACL或继承状态。"""

    def __init__(self, api: Any, kernel: Any) -> None:
        self.api, self.kernel = api, kernel
        self.descriptor = ctypes.c_void_p()
        _configure(api, kernel)
        self.sid = self._current_sid()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{self.sid}D:P(A;;FA;;;{self.sid})(A;;FA;;;SY)",
            1,
            ctypes.byref(self.descriptor),
            None,
        ):
            raise unavailable()
        self.attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), self.descriptor, 0)

    def _sid_text(self, sid: int | ctypes.c_void_p) -> str:
        result = ctypes.c_void_p()
        if not self.api.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise unavailable()
        try:
            if result.value is None:
                raise unavailable()
            return ctypes.wstring_at(result.value)
        finally:
            self.kernel.LocalFree(result)

    def _current_sid(self) -> str:
        token = ctypes.c_void_p()
        if not self.api.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise unavailable()
        try:
            size = ctypes.c_uint32()
            self.api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 1 <= size.value <= 65536:
                raise unavailable()
            buffer = ctypes.create_string_buffer(size.value)
            if not self.api.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
                raise unavailable()
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
            return self._sid_text(sid)
        finally:
            self.kernel.CloseHandle(token)

    def verify(self, handle: int) -> None:
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if self.api.GetSecurityInfo(
            handle,
            1,
            0x5,
            ctypes.byref(owner),
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(descriptor),
        ):
            raise unavailable()
        try:
            if self._sid_text(owner) != self.sid or not acl:
                raise unavailable()
            control, revision = ctypes.c_uint16(), ctypes.c_uint32()
            if (
                not self.api.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                )
                or not control.value & 0x1000
            ):
                raise unavailable()
            self._verify_aces(acl)
        finally:
            self.kernel.LocalFree(descriptor)

    def _verify_aces(self, acl: ctypes.c_void_p) -> None:
        info = _AclSize()
        if not self.api.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise unavailable()
        if info.count != 2:
            raise unavailable()
        identities = set()
        for index in range(info.count):
            pointer = ctypes.c_void_p()
            if not self.api.GetAce(acl, index, ctypes.byref(pointer)):
                raise unavailable()
            ace = ctypes.cast(pointer, ctypes.POINTER(_AllowedAce)).contents
            if ace.kind != 0 or ace.flags != 0 or ace.mask != 0x1F01FF or ace.size < 12:
                raise unavailable()
            identities.add(self._sid_text(ctypes.addressof(ace) + _AllowedAce.sid.offset))
        if identities != {self.sid, "S-1-5-18"}:
            raise unavailable()

    def close(self) -> None:
        if self.descriptor.value:
            self.kernel.LocalFree(self.descriptor)
            self.descriptor.value = None
