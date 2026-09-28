"""Windows Owner的跨平台资源归属与API调用顺序合同；不计作原生Win32证明。"""

from __future__ import annotations

import ctypes
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes.owner_protocol import ProcessOwnerStart
from harnessix.processes.windows_job import WindowsJobObject
from harnessix.processes.windows_owner import _Owner


def _owner(root: Path, descriptor: int) -> _Owner:
    request = ProcessOwnerStart(
        process_id=uuid4(),
        owner_identity="a" * 64,
        owner_token="b" * 64,
        argv=("fixture.exe",),
        cwd=str(root),
        environment={},
        terminal="pipe",
        stdin="closed",
        deadline=datetime.now(UTC) + timedelta(seconds=5),
        output_bytes=4096,
        input_bytes=0,
        columns=80,
        rows=24,
    )
    return _Owner(request, descriptor, bytearray(), root)


@pytest.mark.parametrize("started", [False, True])
def test_control_descriptor_close_has_exactly_one_owner(tmp_path: Path, started: bool) -> None:
    read_fd, write_fd = os.pipe()
    owner = _owner(tmp_path, read_fd)
    owner._control_reader_started = started
    try:
        owner._close()
        if started:
            # 模拟原生退出窗口：Controller仍持有写端时，主线程不能抢占读线程关闭FD。
            os.fstat(read_fd)
            os.close(write_fd)
            write_fd = -1
            owner._reader("control", read_fd, bytearray())
            assert owner.events.get_nowait() == ("control_eof", None)
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        if write_fd >= 0:
            os.close(write_fd)


def test_failed_control_thread_start_keeps_descriptor_with_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_fd, write_fd = os.pipe()
    owner = _owner(tmp_path, read_fd)
    thread = Mock()
    thread.start.side_effect = RuntimeError("thread creation failed")
    monkeypatch.setattr("harnessix.processes.windows_owner.threading.Thread", lambda **_kw: thread)
    try:
        with pytest.raises(RuntimeError):
            owner._start_reader("control", read_fd, bytearray())
        assert owner._control_reader_started is False
        os.fstat(read_fd)
    finally:
        owner._close()
        os.close(write_fd)
    with pytest.raises(OSError):
        os.fstat(read_fd)


@pytest.mark.parametrize("membership", [True, False])
def test_job_membership_is_verified_on_original_handle_before_resume(
    monkeypatch: pytest.MonkeyPatch, membership: bool
) -> None:
    calls = []

    def query(process, job, result):
        calls.append(("query", process, job))
        result._obj.value = membership
        return True

    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=123),
        AssignProcessToJobObject=Mock(side_effect=lambda *_args: calls.append("assign") or True),
        IsProcessInJob=Mock(side_effect=query),
        CloseHandle=Mock(),
    )
    ntdll = SimpleNamespace(
        NtResumeProcess=Mock(side_effect=lambda _h: calls.append("resume") or 0)
    )
    monkeypatch.setattr("harnessix.processes.windows_job._kernel32", lambda: kernel32)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_kw: ntdll, raising=False)
    job = object.__new__(WindowsJobObject)
    job._handle = 456
    if membership:
        job.assign_suspended(789)
        assert calls == ["assign", ("query", 123, 456), "resume"]
    else:
        with pytest.raises(KernelError) as error:
            job.assign_suspended(789)
        assert error.value.code == "process_job_assignment_failed"
        assert calls == ["assign", ("query", 123, 456)]
        ntdll.NtResumeProcess.assert_not_called()
    kernel32.OpenProcess.assert_called_once()
    kernel32.CloseHandle.assert_called_once_with(123)
