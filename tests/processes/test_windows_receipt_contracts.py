"""原回执MAC与序号不变；模拟端口和原生旧读者负对照分别验收。"""

from __future__ import annotations

import os
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


@pytest.mark.skipif(os.name != "nt", reason="原生Windows打开旧Receipt时的名称替换")
def test_windows_old_receipt_reader_keeps_original_mac_while_new_receipt_is_published(tmp_path):
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
        write_owner_receipt(path, second)
        old = ProcessOwnerReceipt.model_validate_json(os.read(reader, 64 * 1024))
        assert verify_owner_receipt(old, owner_token="d" * 64, process_id=identity) == first
        assert read_owner_receipt(path, owner_token="d" * 64, process_id=identity) == second
    finally:
        os.close(reader)
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
