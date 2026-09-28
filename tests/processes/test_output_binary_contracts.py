"""输出必须按原始字节计量、落盘与读取；模拟标志测试不替代原生CRT验证。"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harnessix.processes.owner_output import CapturedProcessOutput
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import (
    PosixProcessSupervisor,
    SupervisedProcess,
    WindowsProcessSupervisor,
)
from tests.processes.test_output_protection import native_plan


@pytest.mark.parametrize("operation", ["capture", "read"])
async def test_output_requests_explicit_binary_mode(tmp_path, monkeypatch, operation):
    binary_flag = getattr(os, "O_BINARY", 1 << 26)
    monkeypatch.setattr(os, "O_BINARY", binary_flag, raising=False)
    original = os.open
    calls = []

    def observed_open(path, flags, *args, **kwargs):
        calls.append(flags)
        # 非Windows宿主仅观察调用合同；未知模拟位不能传给真实系统调用。
        native_flags = flags if os.name == "nt" else flags & ~binary_flag
        return original(path, native_flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", observed_open)
    path = tmp_path / "stdout.bin"
    expected = b"a\r\nb\n\x1ac\x00\xff"
    if operation == "capture":
        capture = CapturedProcessOutput(path, ())
        try:
            capture.feed(expected, len(expected))
            capture.finish(len(expected), eof=True)
            capture.sync()
            assert path.read_bytes() == expected
        finally:
            capture.close()
    else:
        path.write_bytes(expected)
        handle = _output_handle(tmp_path, expected)
        monkeypatch.setattr(handle, "refresh", AsyncMock())
        assert await handle.output("stdout") == expected
    assert len(calls) == 1 and calls[0] & binary_flag


def _output_handle(root: Path, body: bytes) -> SupervisedProcess:
    handle = object.__new__(SupervisedProcess)
    handle._run_directory = root
    handle._lease = SimpleNamespace(
        stdout=SimpleNamespace(
            persisted_bytes=len(body), persisted_sha256=hashlib.sha256(body).hexdigest()
        )
    )
    return handle


@pytest.mark.parametrize("allowance", [0, 1, 4, 128])
@pytest.mark.parametrize("short_write", [False, True])
def test_capture_physical_bytes_match_persisted_observation(
    tmp_path, monkeypatch, allowance, short_write
):
    original = os.write
    if short_write:
        monkeypatch.setattr(os, "write", lambda fd, data: original(fd, data[:1]))
    path = tmp_path / "stdout.bin"
    capture = CapturedProcessOutput(path, ())
    try:
        body = b"LF\nCRLF\r\nCTRL\x1aNUL\x00UTF8" + "汉字".encode() + b"\xff"
        for byte in body:
            capture.feed(bytes([byte]), max(0, allowance - capture.persisted))
        capture.finish(max(0, allowance - capture.persisted), eof=True)
        capture.sync()
        observation = capture.observation()
        physical = path.read_bytes()
        assert physical == body[:allowance]
        assert observation.observed_bytes == len(body)
        assert observation.persisted_bytes == len(physical)
        assert observation.persisted_sha256 == hashlib.sha256(physical).hexdigest()
        assert observation.truncated is (allowance < len(body))
    finally:
        capture.close()


@pytest.mark.skipif(os.name != "nt", reason="原生Windows CRT负对照")
def test_windows_crt_text_mode_changes_the_physical_output(tmp_path):
    body = b"a\nb\r\n"
    path = tmp_path / "text-control.bin"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TEXT, 0o600)
    try:
        assert os.write(fd, body) == len(body)
        os.fsync(fd)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"a\r\nb\r\r\n"


async def test_output_preserves_lf_crlf_control_z_and_invalid_utf8(tmp_path, monkeypatch):
    body = b"a\r\nb\n\x1ac\x00\xff" + "汉字".encode()
    (tmp_path / "stdout.bin").write_bytes(body)
    handle = _output_handle(tmp_path, body)
    monkeypatch.setattr(handle, "refresh", AsyncMock())
    assert await handle.output("stdout") == body


async def test_real_owner_binary_files_and_lease_observations_are_identical(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stdout = b"a\r\nb\n\x1ac\x00\xff" + "汉字".encode()
    stderr = b"err\n\r\n\x1a\x00\xfe"
    code = (
        f"import os; os.write(1,bytes.fromhex('{stdout.hex()}')); "
        f"os.write(2,bytes.fromhex('{stderr.hex()}'))"
    )
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    async with supervisor_type(tmp_path / "state") as supervisor:
        spec = build_process_spec(
            invocation="argv", argv=(sys.executable, "-I", "-c", code), timeout_seconds=5
        )
        handle = await supervisor.start(
            native_plan(workspace, spec, supervisor),
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        lease = await handle.wait()
        assert lease.state == "exited" and lease.returncode == 0
        for stream, expected in (("stdout", stdout), ("stderr", stderr)):
            observation = getattr(lease, stream)
            physical = (handle._run_directory / f"{stream}.bin").read_bytes()
            assert physical == await handle.output(stream) == expected
            assert observation.persisted_bytes == len(physical)
            assert observation.persisted_sha256 == hashlib.sha256(physical).hexdigest()
            assert observation.eof and not observation.truncated
