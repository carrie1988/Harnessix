"""真实Windows pipe/CRT/Job的认证raw观察；其他平台只收集并跳过。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

from harnessix.processes.owner_receipt import ProcessOwnerReceiptV2, read_owner_receipt
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import SupervisedProcess, WindowsProcessSupervisor
from tests.agent.test_publication import CANARY, protected
from tests.processes.test_windows_supervisor import _plan, _wait_stopped

pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实Windows CRT pipe/v2回执/Job契约")

_BINARY_STDIN = (
    "import msvcrt,os,sys\n"
    "for fd in (0,1,2): msvcrt.setmode(fd,os.O_BINARY)\n"
    "assert 'MODEL_KEY' not in os.environ\n"
)


async def _terminal(handle: SupervisedProcess, *, reason: str) -> ProcessOwnerReceiptV2:
    receipt = await handle._terminal_owner_receipt()
    assert isinstance(receipt, ProcessOwnerReceiptV2)
    assert receipt.state == "exited" and receipt.stop_reason == reason
    assert receipt.stdout.eof and receipt.stderr.eof
    assert receipt.raw_stdout.eof and receipt.raw_stderr.eof
    assert receipt.sequence == handle._last_receipt_sequence
    assert receipt.stdout == handle.lease.stdout and receipt.stderr == handle.lease.stderr
    assert handle.lease.pid is not None
    await _wait_stopped(handle.lease.pid)
    return receipt


async def _wait_raw_prefix(handle: SupervisedProcess, stdout: bytes, stderr: bytes) -> None:
    """第二次写入前等待真实Owner签发第一块，不能用sleep假定pipe的分块边界。"""
    for _ in range(500):
        await handle.refresh()
        receipt = await asyncio.to_thread(
            read_owner_receipt,
            handle._run_directory / "receipt.json",
            process_id=handle.lease.process_id,
            owner_identity=handle.lease.owner_identity,
            owner_token=handle.lease.owner_token,
        )
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        if (receipt.raw_stdout.observed_bytes, receipt.raw_stderr.observed_bytes) == (
            len(stdout),
            len(stderr),
        ):
            assert receipt.state == "running"
            assert not receipt.raw_stdout.eof and not receipt.raw_stderr.eof
            assert receipt.raw_stdout.sha256 == hashlib.sha256(stdout).hexdigest()
            assert receipt.raw_stderr.sha256 == hashlib.sha256(stderr).hexdigest()
            return
        assert receipt.state == "running", "等待第一块raw统计时目标进程已退出"
        await asyncio.sleep(0.01)
    raise TimeoutError("Windows Owner未签发跨块同步所需的原始进度")


async def test_windows_pipe_receipt_preserves_lf_crlf_ctrl_z_and_non_utf8(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = b"LF\nCRLF\r\nCTRL-Z\x1aAFTER\xff\x80\x00END\n"
    code = _BINARY_STDIN + (
        "data=sys.stdin.buffer.read()\n"
        "assert os.write(1,data)==len(data)\n"
        "assert os.write(2,data[::-1])==len(data)\n"
    )
    async with WindowsProcessSupervisor(tmp_path / "state") as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(sys.executable, "-I", "-c", code),
            stdin="pipe",
            input_bytes=256,
            output_bytes=4096,
            timeout_seconds=5.0,
        )
        handle = await supervisor.start(
            _plan(workspace, spec, supervisor),
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        await handle.send_stdin(payload)
        await handle.close_stdin()
        lease = await asyncio.wait_for(handle.wait(), timeout=8)
        assert lease.state == "exited" and lease.returncode == 0
        receipt = await _terminal(handle, reason="exited")
        for name, expected in (("stdout", payload), ("stderr", payload[::-1])):
            output = await handle.output(name)
            observation = getattr(receipt, "raw_" + name)
            assert output == expected
            assert observation.observed_bytes == len(expected)
            assert observation.sha256 == hashlib.sha256(expected).hexdigest()
            assert (handle._run_directory / (name + ".bin")).read_bytes() == expected


async def test_windows_secret_across_authenticated_pipe_chunks_keeps_complete_raw(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prefixes = {"stdout": b"\xffLF\nCRLF\r\n\x1a", "stderr": b"\x80\x00\r\n\x1a"}
    suffix = b"\r\n\x1a\xfeEND"
    split = len(CANARY.encode()) // 2
    code = _BINARY_STDIN + (
        "value=sys.stdin.buffer.readline().rstrip(b'\\n')\n"
        f"split={split}\n"
        f"prefixes={prefixes!r}\n"
        f"suffix={suffix!r}\n"
        "for fd,name in ((1,'stdout'),(2,'stderr')):\n"
        " data=prefixes[name]+value[:split]; assert os.write(fd,data)==len(data)\n"
        "assert sys.stdin.buffer.read(1)==b'!'\n"
        "for fd in (1,2):\n"
        " data=value[split:]+suffix; assert os.write(fd,data)==len(data)\n"
    )
    with protected() as scope:
        async with WindowsProcessSupervisor(
            tmp_path / "state", output_redaction=scope
        ) as supervisor:
            spec = build_process_spec(
                invocation="argv",
                argv=(sys.executable, "-I", "-c", code),
                stdin="pipe",
                input_bytes=256,
                output_bytes=4096,
                timeout_seconds=5.0,
            )
            plan = _plan(workspace, spec, supervisor)
            assert not plan.secrets and CANARY not in plan.model_dump_json()
            handle = await supervisor.start(
                plan,
                spec,
                supervisor.capability,
                workspace=workspace,
                environment={},
            )
            await handle.send_stdin(CANARY.encode() + b"\n")
            await _wait_raw_prefix(
                handle,
                prefixes["stdout"] + CANARY.encode()[:split],
                prefixes["stderr"] + CANARY.encode()[:split],
            )
            await handle.send_stdin(b"!")
            await handle.close_stdin()
            lease = await asyncio.wait_for(handle.wait(), timeout=8)
            assert lease.state == "exited" and lease.returncode == 0
            receipt = await _terminal(handle, reason="exited")
            for name, prefix in prefixes.items():
                original = prefix + CANARY.encode() + suffix
                safe = prefix + b"[REDACTED]" + suffix
                assert await handle.output(name) == safe
                raw = getattr(receipt, "raw_" + name)
                published = getattr(receipt, name)
                assert raw.observed_bytes == len(original)
                assert raw.sha256 == hashlib.sha256(original).hexdigest()
                assert published.observed_bytes == published.persisted_bytes == len(safe)
                assert (
                    published.sha256
                    == published.persisted_sha256
                    == hashlib.sha256(safe).hexdigest()
                )
                assert raw.sha256 != published.sha256
            for name in ("stdout.bin", "stderr.bin", "receipt.json"):
                assert CANARY.encode() not in (handle._run_directory / name).read_bytes()


async def test_windows_raw_output_budget_stops_secret_compression_bypass(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repetitions, budget = 4, 64
    original = CANARY.encode() * repetitions
    safe = b"[REDACTED]" * repetitions
    assert len(safe) < budget < len(original)
    code = _BINARY_STDIN + (
        "import time\n"
        "value=sys.stdin.buffer.readline().rstrip(b'\\n')\n"
        f"data=value*{repetitions}\n"
        "assert os.write(1,data)==len(data)\n"
        "time.sleep(30)\n"
    )
    with protected() as scope:
        async with WindowsProcessSupervisor(
            tmp_path / "state", output_redaction=scope
        ) as supervisor:
            spec = build_process_spec(
                invocation="argv",
                argv=(sys.executable, "-I", "-c", code),
                stdin="pipe",
                input_bytes=256,
                output_bytes=budget,
                timeout_seconds=5.0,
            )
            handle = await supervisor.start(
                _plan(workspace, spec, supervisor),
                spec,
                supervisor.capability,
                workspace=workspace,
                environment={},
            )
            await handle.send_stdin(CANARY.encode() + b"\n")
            await handle.close_stdin()
            lease = await asyncio.wait_for(handle.wait(), timeout=8)
            assert lease.state == "exited" and lease.stop_reason == "output_limit"
            receipt = await _terminal(handle, reason="output_limit")
            assert receipt.raw_stdout.observed_bytes == len(original) > budget
            assert receipt.raw_stdout.sha256 == hashlib.sha256(original).hexdigest()
            assert receipt.stdout.observed_bytes == len(safe) < budget
            assert await handle.output("stdout") == safe
            assert await handle.output("stderr") == b""
            assert receipt.raw_stderr.observed_bytes == 0
            assert receipt.raw_stderr.sha256 == hashlib.sha256(b"").hexdigest()
