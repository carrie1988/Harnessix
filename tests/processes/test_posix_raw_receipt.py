"""原 POSIX Owner 的 pipe 原始双流认证与 PTY 回执兼容性验证。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.processes.owner_receipt import (
    OwnerReceipt,
    ProcessOwnerReceipt,
    ProcessOwnerReceiptV2,
    read_owner_receipt,
    verify_owner_receipt,
)
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.processes.supervisor import PosixProcessSupervisor, SupervisedProcess
from harnessix.product_config.git_delivery_process import GitOperationBudget
from tests.processes.helpers import stopped
from tests.processes.test_supervisor import _plan
from tests.product_config import test_git_delivery_process as _git_tests
from tests.product_config.test_git_delivery_process import (
    _marked_pids,
    _tree_program,
)
from tests.product_config.test_git_delivery_process import (
    _plan as _git_plan,
)

# 显式复用原真实端口夹具，避免另建一套进程或授权装配。
git_process_case = _git_tests.make_process

pytestmark = pytest.mark.skipif(os.name != "posix", reason="需要真实 POSIX Process Owner")


@dataclass(frozen=True)
class _Protection:
    value: bytes

    def output_redaction_values(self) -> tuple[bytes, ...]:
        return (self.value,)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "source-workspace"
    workspace.mkdir()
    return workspace


def _spec(code: str, *, input_bytes: int = 0, output_bytes: int = 4096) -> ProcessSpec:
    return build_process_spec(
        invocation="argv",
        argv=(sys.executable, "-I", "-c", code),
        stdin="pipe" if input_bytes else "closed",
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        timeout_seconds=10.0,
    )


async def _start(
    supervisor: PosixProcessSupervisor, workspace: Path, spec: ProcessSpec
) -> SupervisedProcess:
    return await supervisor.start(
        _plan(workspace, spec, supervisor),
        spec,
        supervisor.capability,
        workspace=workspace,
        environment={},
    )


def _read_receipt(state: Path, handle: SupervisedProcess) -> OwnerReceipt:
    lease = handle.lease
    return read_owner_receipt(
        state / "runs" / str(lease.process_id) / "receipt.json",
        owner_token=lease.owner_token,
        process_id=lease.process_id,
        owner_identity=handle._owner_identity,
    )


async def _running_raw_receipt(
    state: Path, handle: SupervisedProcess, stdout_bytes: int, stderr_bytes: int
) -> ProcessOwnerReceiptV2:
    for _ in range(500):
        await handle.refresh()
        receipt = await asyncio.to_thread(_read_receipt, state, handle)
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert receipt.state == "running", "原始输出尚未就绪即进入终态"
        if (
            receipt.raw_stdout.observed_bytes == stdout_bytes
            and receipt.raw_stderr.observed_bytes == stderr_bytes
        ):
            return receipt
        await asyncio.sleep(0.01)
    raise AssertionError("真实 Owner 未发布预期的 running 原始流观察")


def _assert_raw(
    receipt: ProcessOwnerReceiptV2, stdout: bytes, stderr: bytes, *, eof: bool | None = True
) -> None:
    for stream, expected in (("stdout", stdout), ("stderr", stderr)):
        raw = getattr(receipt, f"raw_{stream}")
        assert raw.observed_bytes == len(expected)
        assert raw.sha256 == hashlib.sha256(expected).hexdigest()
        assert raw.eof == getattr(receipt, stream).eof
        if eof is not None:
            assert raw.eof is eof


def _assert_raw_mac(receipt: ProcessOwnerReceiptV2, handle: SupervisedProcess) -> None:
    lease = handle.lease
    assert (
        verify_owner_receipt(
            receipt,
            owner_token=lease.owner_token,
            process_id=lease.process_id,
            owner_identity=handle._owner_identity,
        )
        == receipt
    )
    # 双流的数量、摘要和 EOF 均须受原 MAC 保护；先证明伪造体仍满足结构合同。
    for stream in ("raw_stdout", "raw_stderr"):
        raw = getattr(receipt, stream)
        changes = [{"observed_bytes": raw.observed_bytes + 1}, {"eof": not raw.eof}]
        if raw.observed_bytes:
            changes.append({"sha256": "0" * 64})
        for update in changes:
            forged = receipt.model_copy(update={stream: raw.model_copy(update=update)})
            forged = ProcessOwnerReceiptV2.model_validate_json(forged.model_dump_json())
            with pytest.raises(KernelError) as failure:
                verify_owner_receipt(
                    forged,
                    owner_token=lease.owner_token,
                    process_id=lease.process_id,
                    owner_identity=handle._owner_identity,
                )
            assert failure.value.code == "process_owner_receipt_invalid"


def _writer(stdout: bytes, stderr: bytes, *, wait_for_eof: bool = False) -> str:
    wait = "sys.stdin.buffer.read()" if wait_for_eof else "time.sleep(30)"
    return f"import os,sys,time\nos.write(1,{stdout!r})\nos.write(2,{stderr!r})\n{wait}\n"


async def test_pipe_running_and_exited_separate_raw_binary_from_redacted_artifacts(
    tmp_path: Path,
) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    secret = b"posix-raw-canary"
    first_out, first_err = b"pre:" + secret[:8], b"err:" + secret[:8]
    last_out, last_err = secret[8:] + b":post\0\xff", secret[8:] + b":tail"
    raw_out, raw_err = first_out + last_out, first_err + last_err
    code = (
        f"import os,sys\nos.write(1,{first_out!r})\nos.write(2,{first_err!r})\n"
        "sys.stdin.buffer.read(1)\n"
        f"os.write(1,{last_out!r})\nos.write(2,{last_err!r})\n"
        "sys.stdin.buffer.read()\n"
    )
    async with PosixProcessSupervisor(state, output_redaction=_Protection(secret)) as supervisor:
        handle = await _start(
            supervisor,
            workspace,
            _spec(code, input_bytes=1, output_bytes=len(raw_out) + len(raw_err)),
        )
        running = await _running_raw_receipt(state, handle, len(first_out), len(first_err))
        _assert_raw(running, first_out, first_err, eof=False)
        _assert_raw_mac(running, handle)
        assert running.stdout.observed_bytes == running.stderr.observed_bytes == 0
        await handle.send_stdin(b"1")
        complete_input = await _running_raw_receipt(state, handle, len(raw_out), len(raw_err))
        assert complete_input.sequence > running.sequence
        _assert_raw(complete_input, raw_out, raw_err, eof=False)
        _assert_raw_mac(complete_input, handle)
        await handle.close_stdin()
        lease = await handle.wait()
        receipt = await handle._terminal_owner_receipt()
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert lease.state == receipt.state == "exited" and lease.returncode == 0
        assert lease.stop_reason == "exited" and receipt.sequence > complete_input.sequence
        _assert_raw(receipt, raw_out, raw_err)
        _assert_raw_mac(receipt, handle)
        for stream, raw in (("stdout", raw_out), ("stderr", raw_err)):
            protected = raw.replace(secret, b"[REDACTED]")
            assert await handle.output(stream) == protected
            observation = getattr(receipt, stream)
            assert observation.observed_bytes == observation.persisted_bytes == len(protected)
            assert (
                observation.sha256
                == observation.persisted_sha256
                == hashlib.sha256(protected).hexdigest()
            )
            assert not observation.truncated
            assert observation.sha256 != getattr(receipt, f"raw_{stream}").sha256


async def test_pty_retains_authenticated_v1_receipt_and_merged_output(tmp_path: Path) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    async with PosixProcessSupervisor(state) as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(
                sys.executable,
                "-I",
                "-c",
                "import os; os.write(1,b'out\\n'); os.write(2,b'err\\n')",
            ),
            terminal="pty",
            timeout_seconds=10.0,
            output_bytes=4096,
        )
        handle = await _start(supervisor, workspace, spec)
        lease = await handle.wait()
        receipt = await handle._terminal_owner_receipt()
        assert type(receipt) is ProcessOwnerReceipt
        assert receipt.spec_version == "harnessix.process-owner-receipt/v1"
        assert "raw_stdout" not in receipt.model_dump() and "raw_stderr" not in receipt.model_dump()
        assert lease.state == "exited" and lease.returncode == 0
        assert await handle.output("stdout") == b"out\r\nerr\r\n"
        assert await handle.output("stderr") == b"" and receipt.stderr.eof
        assert (
            verify_owner_receipt(
                receipt,
                owner_token=lease.owner_token,
                process_id=lease.process_id,
                owner_identity=handle._owner_identity,
            )
            == receipt
        )


async def test_launch_failure_authenticates_both_empty_raw_streams(tmp_path: Path) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    async with PosixProcessSupervisor(state) as supervisor:
        spec = build_process_spec(invocation="argv", argv=(str(workspace / "missing-program"),))
        handle = await _start(supervisor, workspace, spec)
        lease = await handle.wait()
        # 原成功投影仅接受 exited；失败事实直接读取原 MAC 回执，不放宽成功端口。
        receipt = await asyncio.to_thread(_read_receipt, state, handle)
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert lease.state == receipt.state == "failed"
        assert lease.stop_reason == receipt.stop_reason == "launch_failed"
        assert lease.pid is receipt.pid is None and receipt.returncode is None
        _assert_raw(receipt, b"", b"")
        _assert_raw_mac(receipt, handle)
        assert await handle.output("stdout") == await handle.output("stderr") == b""


async def test_cancelled_pipe_preserves_authenticated_raw_and_protected_terminal_facts(
    tmp_path: Path,
) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    secret = b"cancel-raw-canary"
    raw_out, raw_err = b"out:" + secret, b"err:" + secret
    async with PosixProcessSupervisor(
        state, output_redaction=_Protection(secret), terminate_grace_seconds=0.05
    ) as supervisor:
        handle = await _start(supervisor, workspace, _spec(_writer(raw_out, raw_err)))
        running = await _running_raw_receipt(state, handle, len(raw_out), len(raw_err))
        _assert_raw_mac(running, handle)
        cancel = CancelToken()
        cancel.cancel()
        lease = await handle.wait(cancel)
        receipt = await handle._terminal_owner_receipt()
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert lease.state == receipt.state == "exited"
        assert lease.stop_reason == receipt.stop_reason == "cancelled"
        _assert_raw(receipt, raw_out, raw_err)
        _assert_raw_mac(receipt, handle)
        assert await handle.output("stdout") == b"out:[REDACTED]"
        assert await handle.output("stderr") == b"err:[REDACTED]"
        assert lease.pid is not None
        await stopped(lease.pid)


async def test_invalid_real_control_frame_settles_unknown_with_raw_fields_in_original_mac(
    tmp_path: Path,
) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    raw_out, raw_err = b"unknown-out\0\xff", b"unknown-err\r\n"
    async with PosixProcessSupervisor(state, terminate_grace_seconds=0.05) as supervisor:
        handle = await _start(supervisor, workspace, _spec(_writer(raw_out, raw_err)))
        running = await _running_raw_receipt(state, handle, len(raw_out), len(raw_err))
        _assert_raw_mac(running, handle)
        # 真实私有管道的协议故障注入，不替换 Owner、Supervisor 或签名实现。
        descriptor = handle._control_fd
        assert descriptor is not None
        invalid = b'{"operation":"invalid"}\n'
        assert await asyncio.to_thread(os.write, descriptor, invalid) == len(invalid)
        lease = await handle.wait()
        receipt = await asyncio.to_thread(_read_receipt, state, handle)
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert lease.state == receipt.state == "unknown"
        assert lease.stop_reason == receipt.stop_reason == "cleanup_failed"
        assert lease.returncode is receipt.returncode is None
        assert receipt.sequence > running.sequence
        _assert_raw(receipt, raw_out, raw_err, eof=None)
        _assert_raw_mac(receipt, handle)
        assert await handle.output("stdout") == raw_out
        assert await handle.output("stderr") == raw_err
        assert lease.pid is not None
        await stopped(lease.pid)


async def test_eof_redaction_tail_expansion_enforces_protected_output_budget(
    tmp_path: Path,
) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    secret = b"abcd"
    # 先关闭输出再保持真实目标存活，隔离 EOF 尾窗限额与退出时 killpg 权限竞态。
    code = (
        "import os,sys,time\nos.write(1,b'abcd')\nsys.stdin.buffer.read()\n"
        "os.close(1)\nos.close(2)\ntime.sleep(30)\n"
    )
    async with PosixProcessSupervisor(state, output_redaction=_Protection(secret)) as supervisor:
        handle = await _start(
            supervisor,
            workspace,
            _spec(code, input_bytes=1, output_bytes=len(secret)),
        )
        running = await _running_raw_receipt(state, handle, len(secret), 0)
        assert running.stdout.observed_bytes == running.stdout.persisted_bytes == 0
        _assert_raw(running, secret, b"", eof=False)
        await handle.close_stdin()
        lease = await handle.wait()
        receipt = await asyncio.to_thread(_read_receipt, state, handle)
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert lease.state == "exited" and lease.stop_reason == "output_limit"
        _assert_raw(receipt, secret, b"")
        _assert_raw_mac(receipt, handle)
        assert receipt.stdout.observed_bytes == len(b"[REDACTED]")
        assert receipt.stdout.persisted_bytes == len(secret) and receipt.stdout.truncated
        assert receipt.stdout.sha256 == hashlib.sha256(b"[REDACTED]").hexdigest()
        assert await handle.output("stdout") == b"[RED"


async def test_raw_output_budget_cannot_be_bypassed_by_redaction_shrinkage(tmp_path: Path) -> None:
    workspace, state = _workspace(tmp_path), tmp_path / "state"
    secret = b"raw-budget-" + b"x" * 60
    raw = secret * 8
    protected = b"[REDACTED]" * 8
    allowance = 128
    assert len(protected) <= allowance < len(raw)
    async with PosixProcessSupervisor(
        state, output_redaction=_Protection(secret), terminate_grace_seconds=0.05
    ) as supervisor:
        handle = await _start(
            supervisor, workspace, _spec(_writer(raw, b""), output_bytes=allowance)
        )
        lease = await handle.wait()
        receipt = await handle._terminal_owner_receipt()
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert lease.state == "exited" and lease.stop_reason == "output_limit"
        _assert_raw(receipt, raw, b"")
        _assert_raw_mac(receipt, handle)
        assert not receipt.stdout.truncated
        assert receipt.stdout.observed_bytes == receipt.stdout.persisted_bytes == len(protected)
        assert await handle.output("stdout") == protected
        assert lease.pid is not None
        await stopped(lease.pid)


async def test_cancellation_during_spawn_thread_drains_one_real_owner_and_process_tree(
    git_process_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = git_process_case(python_code=_tree_program())
    prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(45))
    plan = _git_plan(case, prepared)
    from tests.product_config.test_git_delivery_process import _checkpoint

    entered, release = threading.Event(), threading.Event()
    original = PosixProcessSupervisor._spawn_owner
    calls = 0

    def delayed_spawn(supervisor, read_fd, run_directory):
        nonlocal calls
        calls += 1
        entered.set()
        if not release.wait(10):
            raise TimeoutError("测试启动屏障未释放")
        return original(supervisor, read_fd, run_directory)

    # 只延迟原启动线程；释放后调用原真实 spawn，不替换 Owner 或进程执行。
    monkeypatch.setattr(PosixProcessSupervisor, "_spawn_owner", delayed_spawn)
    task = asyncio.create_task(
        case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5), "未进入原 Supervisor 启动线程"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "启动结算前不得交付外层取消"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 10)
        assert calls == 1
        with SQLiteProcessLeaseStore(
            case.state / "process-owner/process-leases.db", read_only=True
        ) as store:
            lease = store.load(prepared.spec.process_id)
            assert not store.active()
        assert lease.state == "exited" and lease.stop_reason == "cancelled"
        assert lease.pid is not None
        assert len(list((case.state / "process-owner/runs").iterdir())) == 1
        receipt = read_owner_receipt(
            case.state / "process-owner/runs" / str(lease.process_id) / "receipt.json",
            owner_token=lease.owner_token,
            process_id=lease.process_id,
            owner_identity=lease.owner_identity,
        )
        assert isinstance(receipt, ProcessOwnerReceiptV2)
        assert receipt.state == "exited" and receipt.stop_reason == "cancelled"
        for pid in {lease.pid, *_marked_pids(case.workspace)}:
            await stopped(pid)
        # 已真正启动，取消不代表零效果；只验证已知进程终止及没有活动 Lease。
    finally:
        release.set()
        await case.port.aclose()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
