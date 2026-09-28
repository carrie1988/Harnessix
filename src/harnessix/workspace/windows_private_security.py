"""Windows私有对象的原生安全描述符；密钥保持精确非继承合同，状态使用私有继承。"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from typing import Any

from harnessix.agent.errors import KernelError


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


class PrivateWindowsSecurity:
    """拥有短期安全描述符；读取验证从不修改既有Owner、ACL或继承状态。"""

    def __init__(self, api: Any, kernel: Any, *, error: Callable[[], KernelError]) -> None:
        self.api, self.kernel, self._error = api, kernel, error
        self.descriptor = ctypes.c_void_p()
        _configure(api, kernel)
        self.sid = self._current_sid()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self._sddl(),
            1,
            ctypes.byref(self.descriptor),
            None,
        ):
            raise self._error()
        self.attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), self.descriptor, 0)

    def _sid_text(self, sid: int | ctypes.c_void_p) -> str:
        result = ctypes.c_void_p()
        if not self.api.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise self._error()
        try:
            if result.value is None:
                raise self._error()
            return ctypes.wstring_at(result.value)
        finally:
            self.kernel.LocalFree(result)

    def _current_sid(self) -> str:
        return self._token_sid(1)

    def _token_sid(self, information_class: int) -> str:
        token = ctypes.c_void_p()
        if not self.api.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise self._error()
        try:
            size = ctypes.c_uint32()
            self.api.GetTokenInformation(token, information_class, None, 0, ctypes.byref(size))
            if not 1 <= size.value <= 65536:
                raise self._error()
            buffer = ctypes.create_string_buffer(size.value)
            if not self.api.GetTokenInformation(
                token, information_class, buffer, size.value, ctypes.byref(size)
            ):
                raise self._error()
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
            return self._sid_text(sid)
        finally:
            self.kernel.CloseHandle(token)

    def _sddl(self) -> str:
        return f"O:{self.sid}D:P(A;;FA;;;{self.sid})(A;;FA;;;SY)"

    def verify(self, handle: int) -> None:
        """密钥只允许当前用户Owner、protected DACL及两条无继承ACE。"""
        _verify_private(self, handle, owners={self.sid}, forms={(True, 0)})

    def close(self) -> None:
        if self.descriptor.value:
            self.kernel.LocalFree(self.descriptor)
            self.descriptor.value = None


class PrivateStateSecurity(PrivateWindowsSecurity):
    """非密钥状态仅用户/SYSTEM可访问；受管目录显式创建，子文件允许同Token默认Owner。"""

    def __init__(self, api: Any, kernel: Any) -> None:
        super().__init__(api, kernel, error=private_state_error)
        try:
            self.default_owner = self._token_sid(4)
            if self.default_owner not in {self.sid, "S-1-5-32-544"}:
                raise self._error()
        except BaseException:
            self.close()
            raise

    def _sddl(self) -> str:
        return f"O:{self.sid}D:P(A;OICI;FA;;;{self.sid})(A;OICI;FA;;;SY)"

    def verify(self, handle: int) -> None:
        """允许精确私有显式或继承形态；不接受Administrators授权ACE或未知Owner。"""
        _verify_private(
            self,
            handle,
            owners={self.sid, self.default_owner},
            forms={(True, 0), (True, 3), (False, 16), (False, 19)},
        )

    def verify_root(self, handle: int, *, inheritable: bool = False) -> None:
        """状态根必须为当前用户Owner且protected；运行根还必须向子对象继承私有ACL。"""
        forms = {(True, 3)} if inheritable else {(True, 0), (True, 3)}
        _verify_private(self, handle, owners={self.sid}, forms=forms)


def private_state_error() -> KernelError:
    """固定低敏错误不暴露Owner、SID、ACL或实际状态地址。"""
    return KernelError("private_state_invalid", "Windows私有状态权限或身份无效")


def _verify_private(
    security: PrivateWindowsSecurity,
    handle: int,
    *,
    owners: set[str],
    forms: set[tuple[bool, int]],
) -> None:
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    api = security.api
    if api.GetSecurityInfo(
        handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor)
    ):
        raise security._error()
    try:
        if security._sid_text(owner) not in owners or not acl:
            raise security._error()
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        if not api.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise security._error()
        protected = bool(control.value & 0x1000)
        _verify_aces(security, acl, protected=protected, forms=forms)
    finally:
        security.kernel.LocalFree(descriptor)


def _verify_aces(
    security: PrivateWindowsSecurity,
    acl: ctypes.c_void_p,
    *,
    protected: bool,
    forms: set[tuple[bool, int]],
) -> None:
    info, api = _AclSize(), security.api
    if not api.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2):
        raise security._error()
    if info.count != 2:
        raise security._error()
    identities, flags = set(), set()
    for index in range(info.count):
        pointer = ctypes.c_void_p()
        if not api.GetAce(acl, index, ctypes.byref(pointer)):
            raise security._error()
        ace = ctypes.cast(pointer, ctypes.POINTER(_AllowedAce)).contents
        if (
            ace.kind != 0
            or (protected, ace.flags) not in forms
            or ace.mask != 0x1F01FF
            or ace.size < 12
        ):
            raise security._error()
        identities.add(security._sid_text(ctypes.addressof(ace) + _AllowedAce.sid.offset))
        flags.add(ace.flags)
    if identities != {security.sid, "S-1-5-18"} or len(flags) != 1:
        raise security._error()
