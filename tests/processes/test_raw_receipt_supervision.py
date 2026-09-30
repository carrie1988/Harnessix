"""原始统计只能随已认证退出事实读取；跨平台单元验证不替代原生Win32。"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes import windows_owner, windows_owner_observation
from harnessix.processes.owner_receipt import (
    ProcessOwnerReceiptV2,
    RawProcessOutputObservation,
    sign_owner_receipt,
    write_owner_receipt,
)
from harnessix.processes.supervision_contracts import ProcessLease, empty_process_output
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.processes.supervisor import SupervisedProcess
from harnessix.secrets.redaction import StreamingSecretRedactor
from tests.processes.test_windows_owner_lifecycle import _owner


def _finished_handle(root, *, raw, sequence=2):
    now = datetime.now(UTC)
    arguments = dict(
        process_id=uuid4(),
        owner_identity="e" * 64,
        owner_token="d" * 64,
        state="exited",
        sequence=sequence,
        pid=123,
        started_at=now - timedelta(seconds=1),
        finished_at=now,
        returncode=0,
        stop_reason="exited",
        stdout=empty_process_output(eof=True),
        stderr=empty_process_output(eof=True),
    )
    if raw:
        observation = RawProcessOutputObservation(
            observed_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            eof=True,
        )
        arguments.update(raw_stdout=observation, raw_stderr=observation)
    receipt = sign_owner_receipt(**arguments)
    lease = ProcessLease(
        process_id=receipt.process_id,
        plan_id=uuid4(),
        plan_fingerprint="a" * 64,
        process_spec_digest="b" * 64,
        capability_digest="c" * 64,
        launch_binding_digest="f" * 64,
        lifecycle="foreground",
        state="exited",
        sequence=7,
        owner_token="d" * 64,
        owner_identity=receipt.owner_identity,
        pid=receipt.pid,
        deadline=now + timedelta(seconds=5),
        started_at=receipt.started_at,
        finished_at=receipt.finished_at,
        returncode=receipt.returncode,
        stop_reason=receipt.stop_reason,
        stdout=receipt.stdout,
        stderr=receipt.stderr,
    )
    store = Mock(spec=SQLiteProcessLeaseStore)
    handle = SupervisedProcess(store, lease, root, receipt.owner_identity)
    write_owner_receipt(root / "receipt.json", receipt)
    return handle, receipt, store


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("accepted_sequence", [0, 2])
async def test_terminal_receipt_is_reverified_without_lease_transition(
    tmp_path, raw, accepted_sequence
):
    handle, receipt, store = _finished_handle(tmp_path, raw=raw)
    handle._last_receipt_sequence = accepted_sequence
    checked = await handle._terminal_owner_receipt()
    assert checked == receipt
    assert isinstance(checked, ProcessOwnerReceiptV2) is raw
    store.transition.assert_not_called()


@pytest.mark.parametrize(
    "field", ["pid", "started_at", "finished_at", "returncode", "stop_reason", "stdout", "stderr"]
)
async def test_signed_terminal_receipt_must_match_original_lease(tmp_path, field):
    handle, receipt, store = _finished_handle(tmp_path, raw=True)
    changes = {
        "pid": 124,
        "started_at": receipt.started_at + timedelta(milliseconds=1),
        "finished_at": receipt.finished_at + timedelta(milliseconds=1),
        "returncode": 1,
        "stop_reason": "timeout",
        "stdout": empty_process_output(),
        "stderr": empty_process_output(),
    }
    arguments = {
        key: getattr(receipt, key)
        for key in (
            "process_id",
            "owner_identity",
            "state",
            "sequence",
            "pid",
            "started_at",
            "finished_at",
            "returncode",
            "stop_reason",
            "stdout",
            "stderr",
            "raw_stdout",
            "raw_stderr",
        )
    }
    arguments[field] = changes[field]
    replacement = sign_owner_receipt(owner_token="d" * 64, **arguments)
    write_owner_receipt(tmp_path / "receipt.json", replacement)
    with pytest.raises(KernelError) as caught:
        await handle._terminal_owner_receipt()
    assert caught.value.code == "process_owner_receipt_invalid"
    store.transition.assert_not_called()


async def test_terminal_cache_does_not_hide_receipt_replacement(tmp_path):
    handle, receipt, _ = _finished_handle(tmp_path, raw=True)
    assert await handle._terminal_owner_receipt() == receipt
    (tmp_path / "receipt.json").write_bytes(b"{}")
    with pytest.raises(KernelError) as caught:
        await handle._terminal_owner_receipt()
    assert caught.value.code == "process_owner_receipt_invalid"


async def test_terminal_receipt_cannot_advance_accepted_sequence(tmp_path):
    handle, _, _ = _finished_handle(tmp_path, raw=True, sequence=3)
    handle._last_receipt_sequence = 2
    with pytest.raises(KernelError) as caught:
        await handle._terminal_owner_receipt()
    assert caught.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize("reason", [None, "exited", "cancelled", "timeout"])
def test_raw_budget_cannot_be_hidden_by_redaction(tmp_path, reason):
    read_fd, write_fd = os.pipe()
    owner = _owner(tmp_path, read_fd)
    owner.job = Mock()
    owner.request = owner.request.model_copy(update={"output_bytes": 32})
    owner.stop_reason = reason
    try:
        owner.stdout._redactor = StreamingSecretRedactor((b"x" * 64,))
        owner._apply_event("stdout", b"x" * 64)
        assert owner.stdout.raw_observed == 64
        assert owner.stdout.observed <= 32
        expected = "output_limit" if reason in {None, "exited"} else reason
        assert owner.stop_reason == expected
        assert owner.job.terminate.call_count == int(reason in {None, "exited"})
    finally:
        owner._close()
        os.close(write_fd)


@pytest.mark.parametrize("terminal", ["pipe", "pty"])
@pytest.mark.parametrize("name", ["control", "stdout", "stderr"])
def test_binary_mode_is_selected_before_pipe_output_reader_starts(
    tmp_path, monkeypatch, terminal, name
):
    read_fd, write_fd = os.pipe()
    owner = _owner(tmp_path, read_fd)
    owner.request = owner.request.model_copy(update={"terminal": terminal})
    events = []
    thread = Mock()
    thread.start.side_effect = lambda: events.append("start")
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(setmode=lambda fd, mode: events.append((fd, mode)))
    )
    monkeypatch.setattr(
        windows_owner, "os", SimpleNamespace(name="nt", O_BINARY=32768, close=os.close)
    )
    monkeypatch.setattr(windows_owner_observation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_owner_observation, "os", SimpleNamespace(O_BINARY=32768))
    monkeypatch.setattr(windows_owner.threading, "Thread", lambda **_: thread)
    try:
        owner._start_reader(name, read_fd, bytearray())
        assert events == (
            [(read_fd, 32768), "start"] if terminal == "pipe" and name != "control" else ["start"]
        )
        owner._control_reader_started = False
    finally:
        owner._close()
        os.close(write_fd)
