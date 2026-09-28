"""原回执MAC与序号不变；模拟端口和原生旧读者负对照分别验收。"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes import windows_receipt as port
from harnessix.processes.owner_receipt import (
    ProcessOwnerReceipt,
    read_owner_receipt,
    sign_owner_receipt,
    verify_owner_receipt,
    write_owner_receipt,
)
from harnessix.processes.supervision_contracts import empty_process_output


@pytest.mark.parametrize("failure", [None, "ntfs", "create", "write", "rename", "cleanup"])
def test_receipt_publication_closes_resources_and_never_replays_unknown(
    tmp_path, monkeypatch, failure
):
    calls, closed = [], []
    path = tmp_path / "receipt.json"
    root = SimpleNamespace(
        path=path.parent,
        _kernel32=SimpleNamespace(CloseHandle=lambda handle: closed.append("temporary")),
        _open_chain=lambda logical, data: ([1, 2], "fixed-parent"),
        _close_all=lambda chain: closed.append("parents"),
        close=lambda: closed.append("root"),
    )

    def operation(name):
        calls.append(name)
        if failure == name or (failure == "cleanup" and name in {"write", "delete"}):
            raise OSError(32, "fixture")

    def create(candidate):
        assert candidate == path.with_name(f".receipt.json.tmp-{os.getpid()}")
        operation("create")
        return 3

    def write(handle, body):
        assert handle == 3 and body == b"bounded-receipt"
        operation("write")

    def rename(handle, name, *, replace):
        assert handle == 3 and name == "receipt.json" and replace is True
        operation("rename")

    operations = SimpleNamespace(
        require_local_ntfs=lambda handle, anchor: operation("ntfs"),
        create_temporary=create,
        write_and_flush=write,
        rename=rename,
        delete=lambda handle: operation("delete"),
    )
    monkeypatch.setattr(port, "WindowsWorkspaceRoot", lambda parent: root)
    monkeypatch.setattr(port, "WindowsFileOperations", lambda kernel: operations)
    if failure is None:
        port.publish_owner_receipt(path, b"bounded-receipt")
    else:
        with pytest.raises(OSError):
            port.publish_owner_receipt(path, b"bounded-receipt")
    assert calls.count("rename") == (failure in {None, "rename"})
    assert ("delete" in calls) is (failure in {"write", "cleanup"})
    assert closed == (
        ["parents", "root"] if failure in {"ntfs", "create"} else ["temporary", "parents", "root"]
    )


def _receipt(process_id, sequence):
    return sign_owner_receipt(
        process_id=process_id,
        owner_identity="e" * 64,
        state="running",
        sequence=sequence,
        owner_token="d" * 64,
        pid=123,
        started_at=datetime(2026, 9, 28, tzinfo=UTC),
        stdout=empty_process_output(),
        stderr=empty_process_output(),
    )


@pytest.mark.parametrize(
    "scenario", ["success", "body-fails", "missing", "reparse", "directory", "hardlink", "convert"]
)
def test_receipt_reader_releases_exact_descriptor_or_original_handle(
    tmp_path, monkeypatch, scenario
):
    closed, calls = [], []
    path = tmp_path / "receipt.json"
    root = SimpleNamespace(
        path=path.parent,
        _kernel32=SimpleNamespace(CloseHandle=lambda handle: closed.append("native")),
        _open_chain=lambda logical, data: ([1, 2], "fixed-parent"),
        _close_all=lambda chain: closed.append("parents"),
        close=lambda: closed.append("root"),
        _information=lambda handle: SimpleNamespace(
            attributes={"reparse": 0x400, "directory": 0x10}.get(scenario, 0),
            links=2 if scenario == "hardlink" else 1,
        ),
        _final_path=lambda handle: "fixed-receipt",
        _assert_under_root=lambda final: calls.append("under-root"),
    )

    def opened(candidate, *, replace):
        assert candidate == path and replace is True
        calls.append("read-delete-no-write")
        return None if scenario == "missing" else 3

    def converted(handle, flags):
        assert handle == 3 and flags == os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if scenario == "convert":
            raise OSError("synthetic conversion failure")
        return 123

    operations = SimpleNamespace(
        require_local_ntfs=lambda handle, anchor: calls.append("ntfs"),
        open_existing=opened,
    )
    monkeypatch.setattr(port, "WindowsWorkspaceRoot", lambda parent: root)
    monkeypatch.setattr(port, "WindowsFileOperations", lambda kernel: operations)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=converted))
    monkeypatch.setattr(port.os, "close", lambda fd: closed.append("descriptor"))

    def read():
        with port.open_owner_receipt(path) as descriptor:
            assert descriptor == 123
            if scenario == "body-fails":
                raise ValueError("synthetic bounded-body failure")

    if scenario == "success":
        read()
    else:
        with pytest.raises((OSError, ValueError)):
            read()
    first = (
        []
        if scenario == "missing"
        else ["descriptor"]
        if scenario in {"success", "body-fails"}
        else ["native"]
    )
    assert closed == [*first, "parents", "root"]
    assert calls[:2] == ["ntfs", "read-delete-no-write"]


@pytest.mark.skipif(os.name != "nt", reason="原生Windows打开旧Receipt时的名称替换")
def test_windows_uncooperative_crt_reader_is_not_bypassed_or_retried(tmp_path):
    path = tmp_path / "receipt.json"
    identity = uuid4()
    first, second = _receipt(identity, 1), _receipt(identity, 2)
    write_owner_receipt(path, first)
    reader = os.open(path, os.O_RDONLY | os.O_BINARY)
    control = tmp_path / "ordinary-replace-control.json"
    control.write_bytes(second.model_dump_json().encode())
    try:
        # 同一个旧CRT读Handle下，原路径replace必须失败；不能仅依赖竞态偶发触发。
        with pytest.raises(PermissionError):
            os.replace(control, path)
        with pytest.raises(KernelError) as caught:
            write_owner_receipt(path, second)
        assert caught.value.code == "process_owner_receipt_write_failed"
        # 外部不兼容共享是正式拒绝，原临时证据保留，不能用Delete/fallback绕过。
        assert path.with_name(f".receipt.json.tmp-{os.getpid()}").is_file()
        old = ProcessOwnerReceipt.model_validate_json(os.read(reader, 64 * 1024))
        assert verify_owner_receipt(old, owner_token="d" * 64, process_id=identity) == first
        assert read_owner_receipt(path, owner_token="d" * 64, process_id=identity) == first
    finally:
        os.close(reader)


@pytest.mark.skipif(os.name != "nt", reason="原生正式Receipt Reader共享合同")
def test_windows_product_receipt_reader_keeps_original_mac_during_publication(tmp_path):
    path = tmp_path / "receipt.json"
    identity = uuid4()
    first, second = _receipt(identity, 1), _receipt(identity, 2)
    write_owner_receipt(path, first)
    with port.open_owner_receipt(path) as reader:
        write_owner_receipt(path, second)
        old = ProcessOwnerReceipt.model_validate_json(os.read(reader, 64 * 1024))
        assert verify_owner_receipt(old, owner_token="d" * 64, process_id=identity) == first
        assert read_owner_receipt(path, owner_token="d" * 64, process_id=identity) == second
    assert not path.with_name(f".receipt.json.tmp-{os.getpid()}").exists()


@pytest.mark.skipif(os.name != "nt", reason="原生NT回执写入故障")
def test_windows_partial_receipt_write_preserves_original_and_removes_only_own_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "receipt.json"
    identity = uuid4()
    first, second = _receipt(identity, 1), _receipt(identity, 2)
    write_owner_receipt(path, first)
    original = port.WindowsFileOperations.write_and_flush

    def partial_write(self, handle, body):
        original(self, handle, body[:7])
        raise OSError(32, "synthetic write failure")

    monkeypatch.setattr(port.WindowsFileOperations, "write_and_flush", partial_write)
    with pytest.raises(KernelError) as caught:
        write_owner_receipt(path, second)
    assert caught.value.code == "process_owner_receipt_write_failed"
    assert read_owner_receipt(path, owner_token="d" * 64, process_id=identity) == first
    assert not path.with_name(f".receipt.json.tmp-{os.getpid()}").exists()
