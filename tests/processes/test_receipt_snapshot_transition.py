"""回执名称周转仅重取快照；正文、MAC和根边界失败不得变成重试成功。"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes import owner_receipt as reader
from harnessix.processes.owner_receipt import read_owner_receipt, write_owner_receipt
from tests.processes.test_windows_receipt_contracts import _receipt


@pytest.mark.parametrize("changes", [1, 6, 7])
def test_changed_receipt_snapshot_reuses_original_bounded_read_budget(
    tmp_path, monkeypatch, changes
):
    identity = uuid4()
    receipt = _receipt(identity, 2)
    calls, delays = [], []

    def once(path):
        calls.append(path)
        if len(calls) <= changes:
            raise KernelError("process_owner_receipt_changed", "回执名称已周转")
        return receipt

    monkeypatch.setattr(reader, "_read_owner_receipt_once", once)
    monkeypatch.setattr(reader.time, "sleep", delays.append)
    path = tmp_path / "receipt.json"
    if changes == 7:
        with pytest.raises(KernelError) as caught:
            read_owner_receipt(path, owner_token="d" * 64, process_id=identity)
        assert caught.value.code == "process_owner_receipt_invalid"
        assert caught.value.__notes__ == ["receipt_snapshot_changed:attempt=7"]
    else:
        assert read_owner_receipt(path, owner_token="d" * 64, process_id=identity) == receipt
    assert calls == [path] * min(changes + 1, 7)
    assert delays == list(reader._WINDOWS_RECEIPT_READ_DELAYS[1 : len(calls)])


@pytest.mark.parametrize("failure", ["json", "mac", "process", "owner", "root"])
def test_changed_snapshot_does_not_retry_later_invalid_binding(tmp_path, monkeypatch, failure):
    identity = uuid4()
    receipt = _receipt(identity, 2)
    calls = []

    def once(path):
        calls.append(path)
        if len(calls) == 1:
            raise KernelError("process_owner_receipt_changed", "回执名称已周转")
        if failure == "json":
            raise ValueError("private-body-must-not-appear")
        if failure == "root":
            raise ValueError("private-root-must-not-appear")
        return receipt.model_copy(
            update={
                "mac": "0" * 64 if failure == "mac" else receipt.mac,
                "process_id": uuid4() if failure == "process" else identity,
                "owner_identity": "f" * 64 if failure == "owner" else receipt.owner_identity,
            }
        )

    monkeypatch.setattr(reader, "_read_owner_receipt_once", once)
    monkeypatch.setattr(reader.time, "sleep", lambda _: None)
    with pytest.raises(KernelError) as caught:
        read_owner_receipt(
            tmp_path / "receipt.json",
            owner_token="d" * 64,
            process_id=identity,
            owner_identity="e" * 64,
        )
    assert caught.value.code == "process_owner_receipt_invalid"
    assert len(calls) == 2
    assert "private" not in str(caught.value)
    assert all("private" not in note for note in getattr(caught.value, "__notes__", []))


@pytest.mark.skipif(os.name != "nt", reason="原生NTFS打开后立即替换Receipt")
@pytest.mark.parametrize("barrier", ["opened", "metadata"])
def test_native_receipt_replacement_between_open_and_binding(tmp_path, monkeypatch, barrier):
    from harnessix.processes import windows_receipt as port

    identity = uuid4()
    path = tmp_path / "receipt.json"
    first, second = _receipt(identity, 1), _receipt(identity, 2)
    write_owner_receipt(path, first)
    opened = port.WindowsFileOperations.open_existing
    information = port.WindowsWorkspaceRoot._information
    state = {"handle": None, "fired": False}

    def replace_once():
        if not state["fired"]:
            state["fired"] = True
            write_owner_receipt(path, second)

    def open_then_replace(self, candidate, **kwargs):
        handle = opened(self, candidate, **kwargs)
        if candidate == Path(os.path.abspath(path)) and not state["fired"]:
            state["handle"] = handle
            if barrier == "opened":
                replace_once()
        return handle

    def metadata_then_replace(self, handle):
        result = information(self, handle)
        if handle == state["handle"] and barrier == "metadata":
            replace_once()
        return result

    monkeypatch.setattr(port.WindowsFileOperations, "open_existing", open_then_replace)
    monkeypatch.setattr(port.WindowsWorkspaceRoot, "_information", metadata_then_replace)
    checked = read_owner_receipt(path, owner_token="d" * 64, process_id=identity)
    assert state["fired"] is True
    # 允许已绑定且MAC合法的旧快照；失去名称的未绑定快照只能重取合法新名称。
    assert checked in (first, second)
    assert read_owner_receipt(path, owner_token="d" * 64, process_id=identity) == second
    assert not path.with_name(f".receipt.json.tmp-{os.getpid()}").exists()


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("zero", "changed"),
        ("hardlink", "invalid"),
        ("reparse-zero", "invalid"),
        ("directory-zero", "invalid"),
        ("outside", "invalid"),
        ("path-io-live", "io"),
        ("path-io-unlinked", "changed"),
        ("path-outside-unlinked", "changed"),
    ],
)
def test_receipt_snapshot_classifier_never_accepts_unlinked_or_unsafe_handle(scenario, expected):
    from types import SimpleNamespace

    from harnessix.processes import windows_receipt as port

    observations = []

    def information(handle):
        assert handle == 7
        observations.append(handle)
        return SimpleNamespace(
            attributes={"reparse-zero": 0x400, "directory-zero": 0x10}.get(scenario, 0),
            links=(
                0
                if scenario in {"zero", "reparse-zero", "directory-zero"}
                or (len(observations) > 1 and scenario.endswith("unlinked"))
                else 2
                if scenario == "hardlink"
                else 1
            ),
        )

    def final_path(handle):
        assert handle == 7
        if scenario.startswith("path-io"):
            raise OSError(2, "private-root-do-not-export")
        return "private-root-do-not-export"

    def under_root(_path):
        raise KernelError("workspace_path_denied", "private-root-do-not-export")

    root = SimpleNamespace(
        _information=information,
        _final_path=final_path,
        _assert_under_root=under_root,
    )
    with pytest.raises((KernelError, ValueError, OSError)) as caught:
        port._verify_receipt_handle(root, 7)
    if expected == "changed":
        assert isinstance(caught.value, KernelError)
        assert caught.value.code == "process_owner_receipt_changed"
        assert "private" not in str(caught.value)
    elif expected == "io":
        assert isinstance(caught.value, OSError)
    else:
        assert (
            not isinstance(caught.value, KernelError)
            or caught.value.code != "process_owner_receipt_changed"
        )


@pytest.mark.skipif(os.name != "nt", reason="原生NTFS旧绑定规则的确定性负对照")
def test_native_original_receipt_binding_rejects_atomic_turnover_snapshot(tmp_path, monkeypatch):
    from harnessix.processes import windows_receipt as port

    identity = uuid4()
    path = tmp_path / "receipt.json"
    first, second = _receipt(identity, 1), _receipt(identity, 2)
    write_owner_receipt(path, first)
    original_open = port.WindowsFileOperations.open_existing
    changed, observed_links = [], []

    def original_binding(root, handle):
        # 固定原实现的三项核验，形成原生旧规则负对照；不作为生产fallback。
        info = root._information(handle)
        observed_links.append(info.links)
        if info.attributes & (0x400 | 0x10) or info.links != 1:
            raise ValueError("原绑定规则拒绝")
        root._assert_under_root(root._final_path(handle))

    def turnover(self, candidate, **kwargs):
        handle = original_open(self, candidate, **kwargs)
        if candidate == Path(os.path.abspath(path)) and not changed:
            changed.append(True)
            write_owner_receipt(path, second)
        return handle

    monkeypatch.setattr(port, "_verify_receipt_handle", original_binding)
    monkeypatch.setattr(port.WindowsFileOperations, "open_existing", turnover)
    with pytest.raises(KernelError) as caught:
        read_owner_receipt(path, owner_token="d" * 64, process_id=identity)
    assert caught.value.code == "process_owner_receipt_invalid"
    assert changed == [True]
    assert observed_links == [0]
