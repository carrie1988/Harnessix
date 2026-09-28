"""受管Windows状态目录的私有创建；原Handle链验权，既有宽权限目录只拒绝、不修复。"""

from __future__ import annotations

import ctypes
import os
from contextlib import ExitStack
from pathlib import Path

from harnessix.workspace.windows import WindowsWorkspaceRoot, _api_path
from harnessix.workspace.windows_private_security import PrivateStateSecurity, private_state_error


def private_state_directory(path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
    """只在已锚定父链创建用户/SYSTEM私有可继承目录；不修改任何存量安全描述符。"""
    if not path.is_absolute():
        raise private_state_error()
    missing: list[Path] = []
    parent = path.parent
    if parents:
        while not parent.exists():
            if os.path.lexists(parent) or parent.parent == parent:
                raise private_state_error()
            missing.append(parent)
            parent = parent.parent
    for candidate in (*reversed(missing), path):
        _create_directory(candidate, exist_ok=exist_ok if candidate == path else True)


def _create_directory(path: Path, *, exist_ok: bool) -> None:
    with ExitStack() as resources:
        root = WindowsWorkspaceRoot(path.parent)
        resources.callback(root.close)
        kernel = root._kernel32
        api = ctypes.__dict__["WinDLL"]("advapi32", use_last_error=True)
        security = PrivateStateSecurity(api, kernel)
        resources.callback(security.close)
        kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        kernel.CreateDirectoryW.restype = ctypes.c_int
        if not kernel.CreateDirectoryW(_api_path(path), ctypes.byref(security.attributes)):
            code = ctypes.__dict__["get_last_error"]()
            if code != 183 or not exist_ok:
                raise FileExistsError if code == 183 else private_state_error()
        chain, _ = root._open_chain(path.name, data=False)
        resources.callback(root._close_all, chain)
        handle = kernel.CreateFileW(_api_path(path), 0x20080, 1, None, 3, 0x2200000, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise private_state_error()
        resources.callback(kernel.CloseHandle, handle)
        info = root._information(handle)
        if info.attributes & 0x400 or not info.attributes & 0x10:
            raise private_state_error()
        if root._object_identity(info) != root._object_identity(root._information(chain[-1])):
            raise private_state_error()
        security.verify_root(int(handle), inheritable=True)
