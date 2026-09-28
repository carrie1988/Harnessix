"""Windows失败诊断只读取有界Owner/DACL角色；不输出SID、正文、密钥或绝对路径。"""

from __future__ import annotations

import ctypes
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
from harnessix.product_config.session_key_windows_security import _AclSize, _AllowedAce
from harnessix.product_config.state_backup_contracts import DATABASES, KEY_FILE, PROCESS_DATABASE
from harnessix.workspace.windows import WindowsWorkspaceRoot, _api_path


def _role(files: WindowsKeyFiles, sid: int | ctypes.c_void_p) -> str:
    identity = files.security._sid_text(sid)
    return {
        files.security.sid: "current_user",
        "S-1-5-18": "system",
        "S-1-5-32-544": "administrators",
        "S-1-3-4": "owner_rights",
    }.get(identity, "other")


def _default_owner(files: WindowsKeyFiles) -> str:
    api, kernel = files.security.api, files.kernel
    token, size = ctypes.c_void_p(), ctypes.c_uint32()
    if not api.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise AssertionError("原生Token查询失败")
    try:
        api.GetTokenInformation(token, 4, None, 0, ctypes.byref(size))
        assert 1 <= size.value <= 65536
        buffer = ctypes.create_string_buffer(size.value)
        assert api.GetTokenInformation(token, 4, buffer, size.value, ctypes.byref(size))
        return _role(files, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents)
    finally:
        kernel.CloseHandle(token)


def _permission(files: WindowsKeyFiles, handle: int) -> dict[str, Any]:
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    api = files.security.api
    assert (
        api.GetSecurityInfo(
            handle,
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(descriptor),
        )
        == 0
    )
    try:
        control, revision = ctypes.c_uint16(), ctypes.c_uint32()
        assert api.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        )
        facts: dict[str, Any] = {
            "owner_role": _role(files, owner),
            "dacl_present": bool(acl),
            "dacl_protected": bool(control.value & 0x1000),
            "aces": [],
        }
        if not acl:
            return facts
        info = _AclSize()
        assert api.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2)
        assert info.count <= 16
        for index in range(info.count):
            pointer = ctypes.c_void_p()
            assert api.GetAce(acl, index, ctypes.byref(pointer))
            ace = ctypes.cast(pointer, ctypes.POINTER(_AllowedAce)).contents
            assert ace.size >= 12 and ace.kind in {0, 1}
            facts["aces"].append(
                {
                    "kind": ace.kind,
                    "flags": ace.flags,
                    "mask": ace.mask,
                    "principal_role": _role(files, ctypes.addressof(ace) + _AllowedAce.sid.offset),
                }
            )
        return facts
    finally:
        files.kernel.LocalFree(descriptor)


def state_permission_facts(state: Path) -> dict[str, Any]:
    """仅在原生Windows失败时检查固定受管样本；原安全验证器和原异常保持不变。"""
    with ExitStack() as resources:
        root = WindowsWorkspaceRoot(state)
        resources.callback(root.close)
        files = WindowsKeyFiles(root)
        resources.callback(files.close)
        samples = [
            ("root", "."),
            *((path, path) for path in (*DATABASES, PROCESS_DATABASE, "session-auth", KEY_FILE)),
            ("process-owner", "process-owner"),
            ("process-runs", "process-owner/runs"),
        ]
        runs = sorted((state / "process-owner/runs").iterdir())
        assert len(runs) <= 16
        if runs:
            prefix = runs[0].relative_to(state).as_posix()
            samples += [("process-run", prefix)]
            samples += [
                (name, f"{prefix}/{name}") for name in ("receipt.json", "stdout.bin", "stderr.bin")
            ]
        output = {}
        for label, relative in samples:
            with ExitStack() as handles:
                chain, _ = root._open_chain(relative, data=False)
                handles.callback(root._close_all, chain)
                handle = files.kernel.CreateFileW(
                    _api_path(state / relative), 0x20080, 1, None, 3, 0x2200000, None
                )
                assert handle is not None and handle != ctypes.c_void_p(-1).value
                handles.callback(files.kernel.CloseHandle, handle)
                assert root._object_identity(root._information(handle)) == root._object_identity(
                    root._information(chain[-1])
                )
                output[label] = _permission(files, int(handle))
        return {
            "spec_version": "harnessix.windows-state-permission-diagnostic/v1",
            "token_default_owner_role": _default_owner(files),
            "objects": output,
        }
