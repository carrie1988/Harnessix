from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import windows_io
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.trusted_action import resolve_workspace_patch
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.delivery.windows_io import WindowsFileOperations, _rename_buffer, _RenameInfo


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("name", ["target.py", "项目 😀.py"])
def test_rename_abi_uses_relative_parent_handle_and_exact_utf16(name: str, replace: bool) -> None:
    buffer = _rename_buffer(123, name, replace=replace)
    header = _RenameInfo.from_buffer(buffer)
    body = name.encode("utf-16-le")
    assert _RenameInfo.name.offset == (20 if ctypes.sizeof(ctypes.c_void_p) == 8 else 12)
    assert header.root == 123 and header.flags == (3 if replace else 0)
    assert header.name_bytes == len(body)
    assert buffer.raw[_RenameInfo.name.offset : _RenameInfo.name.offset + len(body)] == body


def test_partial_writes_are_completed_before_single_flush() -> None:
    chunks: list[bytes] = []
    flushes: list[int] = []

    def write(_handle, buffer, size, written, _overlapped):
        count = min(size, 13)
        chunks.append(buffer.raw[:count])
        ctypes.cast(written, ctypes.POINTER(ctypes.c_uint32)).contents.value = count
        return 1

    operations = WindowsFileOperations.__new__(WindowsFileOperations)
    operations.kernel = SimpleNamespace(
        WriteFile=write, FlushFileBuffers=lambda handle: flushes.append(handle) or 1
    )
    body = bytes(range(256)) * 257
    operations.write_and_flush(17, body)
    assert b"".join(chunks) == body and flushes == [17]


def test_zero_length_native_write_never_flushes_or_loops() -> None:
    def write(_handle, _buffer, _size, written, _overlapped):
        ctypes.cast(written, ctypes.POINTER(ctypes.c_uint32)).contents.value = 0
        return 1

    operations = WindowsFileOperations.__new__(WindowsFileOperations)
    operations.kernel = SimpleNamespace(WriteFile=write, FlushFileBuffers=lambda _: pytest.fail())
    with pytest.raises(OSError, match="长度无效"):
        operations.write_and_flush(1, b"must-not-loop")


@pytest.mark.parametrize("build, expected", [(19045, False), (22000, True), (26100, True)])
def test_native_advertisement_requires_supported_windows_build(
    monkeypatch: pytest.MonkeyPatch, build: int, expected: bool
) -> None:
    monkeypatch.setattr(windows_io, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        sys, "getwindowsversion", lambda: SimpleNamespace(build=build), raising=False
    )
    assert windows_io.windows_transaction_supported() is expected


def test_windows_executable_mode_is_rejected_before_snapshot_or_action_resolution(tmp_path) -> None:
    with pytest.raises(KernelError) as planning:
        prepare_workspace_transaction(
            tmp_path,
            {"app.py": DesiredWorkspaceFile(b"x", 0o755)},
            request_id="windows-mode",
            platform="windows",
        )
    proposal = WorkspacePatchInput(
        files=(WorkspacePatchFile(operation="create", path="app.py", content="x", mode=0o755),)
    )
    with pytest.raises(KernelError) as routing:
        resolve_workspace_patch(proposal, "windows")
    assert planning.value.code == routing.value.code == "delivery_metadata_unsupported"
