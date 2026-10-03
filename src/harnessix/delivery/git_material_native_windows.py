"""Git材料的Windows本地NTFS句柄：文件不共享写／删除，目录不共享删除。"""

from __future__ import annotations

import ctypes
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NoReturn

from harnessix.delivery.git_material_input_contracts import GitMaterialInputError


def _fail(code: str = "git_material_worker_invalid") -> NoReturn:
    raise GitMaterialInputError(code)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("written", _FileTime),
        ("volume", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32),
        ("index_low", ctypes.c_uint32),
    ]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("used", ctypes.c_uint32), ("free", ctypes.c_uint32)]


class _Ace(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_ubyte),
        ("flags", ctypes.c_ubyte),
        ("size", ctypes.c_uint16),
        ("mask", ctypes.c_uint32),
        ("sid", ctypes.c_uint32),
    ]


def _libraries() -> tuple[Any, Any]:
    if os.name != "nt":
        _fail("git_material_platform_unsupported")
    kernel = ctypes.__dict__["WinDLL"]("kernel32", use_last_error=True)
    security = ctypes.__dict__["WinDLL"]("advapi32", use_last_error=True)
    p, u, b = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        "CreateFileW": ([ctypes.c_wchar_p, u, u, p, u, u, p], p),
        "CloseHandle": ([p], b),
        "GetFileInformationByHandle": ([p, ctypes.POINTER(_FileInformation)], b),
        "GetFinalPathNameByHandleW": ([p, ctypes.c_wchar_p, u, u], u),
        "GetDriveTypeW": ([ctypes.c_wchar_p], u),
        "GetVolumeInformationByHandleW": (
            [p, ctypes.c_wchar_p, u, p, p, p, ctypes.c_wchar_p, u],
            b,
        ),
        "GetCurrentProcess": ([], p),
        "DuplicateHandle": ([p, p, p, ctypes.POINTER(p), u, b, u], b),
        "LocalFree": ([p], p),
    }
    for name, (arguments, result) in signatures.items():
        getattr(kernel, name).argtypes = arguments
        getattr(kernel, name).restype = result
    signatures = {
        "OpenProcessToken": ([p, u, ctypes.POINTER(p)], b),
        "GetTokenInformation": ([p, b, p, u, ctypes.POINTER(u)], b),
        "ConvertSidToStringSidW": ([p, ctypes.POINTER(p)], b),
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
        getattr(security, name).argtypes = arguments
        getattr(security, name).restype = result
    return kernel, security


class _Windows:
    """仅提供本次固定材料的有限句柄职责，不构造新 Owner/平台。"""

    def __init__(self) -> None:
        self.kernel, self.security = _libraries()
        self.sid = _token_sid(self, 1)
        self.default_owner = _token_sid(self, 4)
        if self.default_owner not in {self.sid, "S-1-5-32-544"}:
            _fail("git_material_private_invalid")

    def private(self, handle: int, *, directory: bool) -> None:
        _private(self, handle, directory=directory)

    def open(
        self,
        path: Path,
        resources: ExitStack,
        *,
        directory: bool,
        private: bool = False,
        delete_on_close: bool = False,
    ) -> int:
        return _open(
            self,
            path,
            resources,
            directory=directory,
            private=private,
            delete_on_close=delete_on_close,
        )

    def chain(
        self, path: Path, resources: ExitStack, cache: dict[Path, int], *, private: bool = False
    ) -> int:
        return _chain(self, path, resources, cache, private=private)

    def descriptor(
        self, path: Path, resources: ExitStack, *, private: bool, delete_on_close: bool = False
    ) -> int:
        return _descriptor(self, path, resources, private=private, delete_on_close=delete_on_close)

    def snapshot_writer(self, path: Path, resources: ExitStack) -> int:
        return _snapshot_writer(self, path, resources)

    def snapshot_reader(self, writer: int, resources: ExitStack) -> int:
        return _snapshot_reader(self, writer, resources)


def _sid(api: _Windows, pointer: object) -> str:
    text = ctypes.c_void_p()
    if not api.security.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
        _fail("git_material_private_invalid")
    try:
        if text.value is None:
            _fail("git_material_private_invalid")
        return ctypes.wstring_at(text.value)
    finally:
        api.kernel.LocalFree(text)


def _token_sid(api: _Windows, kind: int) -> str:
    token, size = ctypes.c_void_p(), ctypes.c_uint32()
    if not api.security.OpenProcessToken(api.kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        _fail("git_material_private_invalid")
    try:
        api.security.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not 1 <= size.value <= 65536:
            _fail("git_material_private_invalid")
        buffer = ctypes.create_string_buffer(size.value)
        if not api.security.GetTokenInformation(
            token, kind, buffer, size.value, ctypes.byref(size)
        ):
            _fail("git_material_private_invalid")
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        return _sid(api, pointer)
    finally:
        api.kernel.CloseHandle(token)


def _private(api: _Windows, handle: int, *, directory: bool) -> None:
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    security = api.security
    if security.GetSecurityInfo(
        handle,
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(acl),
        None,
        ctypes.byref(descriptor),
    ):
        _fail("git_material_private_invalid")
    try:
        if not acl or _sid(api, owner) not in (
            {api.sid} if directory else {api.sid, api.default_owner}
        ):
            _fail("git_material_private_invalid")
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        if not security.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            _fail("git_material_private_invalid")
        protected = bool(control.value & 0x1000)
        info = _AclSize()
        if (
            not security.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2)
            or info.count != 2
        ):
            _fail("git_material_private_invalid")
        identities, flags = set(), set()
        forms = (
            {(True, 0), (True, 3)}
            if directory
            else {(True, 0), (True, 3), (False, 16), (False, 19)}
        )
        for index in range(info.count):
            pointer = ctypes.c_void_p()
            if not security.GetAce(acl, index, ctypes.byref(pointer)):
                _fail("git_material_private_invalid")
            ace = ctypes.cast(pointer, ctypes.POINTER(_Ace)).contents
            if (
                ace.kind != 0
                or ace.mask != 0x1F01FF
                or ace.size < 12
                or (protected, ace.flags) not in forms
            ):
                _fail("git_material_private_invalid")
            identities.add(_sid(api, ctypes.addressof(ace) + _Ace.sid.offset))
            flags.add(ace.flags)
        if identities != {api.sid, "S-1-5-18"} or len(flags) != 1:
            _fail("git_material_private_invalid")
    finally:
        api.kernel.LocalFree(descriptor)


def _held_access(*, private: bool = False, delete_on_close: bool = False) -> int:
    """持有保护必须参与共享检查：READ_DATA 在目录上对应 LIST_DIRECTORY。"""
    return 0x81 | (0x20000 if private else 0) | (0x10000 if delete_on_close else 0)


def _held_share(*, directory: bool) -> int:
    """目录允许对象发布所需的写共享；既有文件禁写，两者始终禁删除共享。"""
    return 3 if directory else 1


def _open(
    api: _Windows,
    path: Path,
    resources: ExitStack,
    *,
    directory: bool,
    private: bool = False,
    delete_on_close: bool = False,
) -> int:
    before = os.lstat(path)
    access = _held_access(private=private, delete_on_close=delete_on_close)
    flags = 0x2200000 | (0x4000000 if delete_on_close else 0)
    # 仍请求真实数据读取；目录FILE_ADD_FILE打开需要写共享，不授予当前句柄写权限。
    handle = api.kernel.CreateFileW(
        _api_path(path), access, _held_share(directory=directory), None, 3, flags, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        _fail("git_material_binding_changed")
    handle = int(handle)
    resources.callback(api.kernel.CloseHandle, handle)
    info = _FileInformation()
    if not api.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        _fail("git_material_binding_changed")
    if (
        info.attributes & 0x400
        or bool(info.attributes & 0x10) != directory
        or (not directory and info.links != 1)
    ):
        _fail("git_material_binding_changed")
    if ((info.index_high << 32) | info.index_low) != before.st_ino:
        _fail("git_material_binding_changed")
    buffer = ctypes.create_unicode_buffer(32768)
    length = api.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not 0 < length < len(buffer) or buffer.value.casefold() != _api_path(path).casefold():
        _fail("git_material_binding_changed")
    if private:
        api.private(handle, directory=directory)
    return handle


def _chain(
    api: _Windows,
    path: Path,
    resources: ExitStack,
    cache: dict[Path, int],
    *,
    private: bool = False,
) -> int:
    current = Path(path.anchor)
    for part in (path.anchor, *path.parts[1:]):
        if part != path.anchor:
            current /= part
        handle = cache.get(current)
        private_target = private and current == path
        if handle is None:
            handle = api.open(current, resources, directory=True, private=private_target)
            cache[current] = handle
        elif private_target:
            # 旧链守卫可能只有LIST_DIRECTORY/READ_ATTRIBUTES，不能用于读取DACL。
            # 保留原守卫，另开含READ_CONTROL的私有读句柄；不升级写/删除权限或忽略ACL。
            handle = api.open(current, resources, directory=True, private=True)
    filesystem = ctypes.create_unicode_buffer(32)
    if (
        api.kernel.GetDriveTypeW(path.anchor) != 3
        or not api.kernel.GetVolumeInformationByHandleW(
            handle, None, 0, None, None, None, filesystem, len(filesystem)
        )
        or filesystem.value.upper() != "NTFS"
    ):
        _fail("git_material_platform_unsupported")
    assert handle is not None
    return handle


def _descriptor(
    api: _Windows, path: Path, resources: ExitStack, *, private: bool, delete_on_close: bool = False
) -> int:
    import msvcrt

    handles = ExitStack()
    try:
        handle = api.open(
            path,
            handles,
            directory=False,
            private=private,
            delete_on_close=delete_on_close,
        )
        descriptor = int(
            msvcrt.__dict__["open_osfhandle"](handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        )
    except BaseException:
        handles.close()
        raise
    handles.pop_all()
    resources.callback(os.close, descriptor)
    return descriptor


def _api_path(path: Path) -> str:
    value = str(path)
    if not path.drive or value.startswith("\\\\"):
        _fail("git_material_platform_unsupported")
    if any(part.endswith((".", " ")) or ":" in part for part in path.parts[1:]):
        _fail("git_material_binding_changed")
    return "\\\\?\\" + value


def _owned_descriptor(api: _Windows, handle: int, resources: ExitStack, flags: int) -> int:
    """原生 Handle 的所有权只转交 CRT 一次；失败仍关闭原 Handle。"""
    import msvcrt

    handles = ExitStack()
    handles.callback(api.kernel.CloseHandle, handle)
    try:
        descriptor = int(
            msvcrt.__dict__["open_osfhandle"](handle, flags | getattr(os, "O_BINARY", 0))
        )
    except BaseException:
        handles.close()
        raise
    handles.pop_all()
    resources.callback(os.close, descriptor)
    return descriptor


def _snapshot_writer(api: _Windows, path: Path, resources: ExitStack) -> int:
    """从 CREATE_NEW 起绑定 DELETE_ON_CLOSE；未写正文即已受内核关闭删除保护。"""
    # GENERIC_READ | GENERIC_WRITE | DELETE；仅共享读，句柄不得被子进程继承。
    handle = api.kernel.CreateFileW(_api_path(path), 0xC0010000, 1, None, 1, 0x04200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        _fail("git_material_binding_changed")
    handles = ExitStack()
    handles.callback(api.kernel.CloseHandle, handle)
    try:
        info = _FileInformation()
        if not api.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            _fail("git_material_binding_changed")
        if info.attributes & 0x410 or info.links != 1:
            _fail("git_material_binding_changed")
        api.private(int(handle), directory=False)
    except BaseException:
        handles.close()
        raise
    handles.pop_all()
    return _owned_descriptor(api, int(handle), resources, os.O_WRONLY)


def _snapshot_reader(api: _Windows, writer: int, resources: ExitStack) -> int:
    """降权复制同一 FILE_OBJECT：最后 RO 引用关闭时仍执行删除，不再按路径重开。"""
    import msvcrt

    source = int(msvcrt.__dict__["get_osfhandle"](writer))
    target = ctypes.c_void_p()
    current = api.kernel.GetCurrentProcess()
    # FILE_GENERIC_READ=READ_DATA|READ_EA|READ_ATTRIBUTES|READ_CONTROL|SYNCHRONIZE。
    # 不含 WRITE_DATA/APPEND_DATA/WRITE_EA/WRITE_ATTRIBUTES/DELETE，也不使用 SAME_ACCESS。
    if not api.kernel.DuplicateHandle(
        current, source, current, ctypes.byref(target), 0x120089, False, 0
    ):
        _fail("git_material_binding_changed")
    if target.value is None or target.value == ctypes.c_void_p(-1).value:
        _fail("git_material_binding_changed")
    return _owned_descriptor(api, target.value, resources, os.O_RDONLY)
