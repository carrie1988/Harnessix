"""真实 POSIX Supervisor 启动半窗；不覆盖认证父 Session 链，不构成发布验收。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import execution_is_approved
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes import supervisor as process_module
from harnessix.processes.owner_protocol import ProcessOwnerCommand, decode_owner_start
from harnessix.processes.owner_receipt import read_owner_receipt
from harnessix.processes.supervisor import PosixProcessSupervisor, SupervisedProcess
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from harnessix.product_config.git_process_host import (
    GitProcessRuntimeHost,
    save_git_process_plan,
    start_git_process,
)
from harnessix.product_config.state_owner import product_state_owner
from harnessix.secrets.publication import SecretPublicationScope
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs

make_process = inputs.make_process
pytestmark = pytest.mark.skipif(os.name != "posix", reason="需要真实 POSIX Process Owner")

_PROGRAM = "import time; time.sleep(5)"
_WAIT_SECONDS = 5
_GATE_SECONDS = 10


def _fd_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        return False
    return True


class _RealStartWindow:
    """仅观察真实资源，并在原控制写入完成后阻止线程函数返回。"""

    def __init__(self, supervisor, monkeypatch, *, block_write: bool, block_spawn: bool = False):
        self.supervisor = supervisor
        self.block_write = block_write
        self.entered = threading.Event()
        self.spawned = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()
        self.gate_timed_out = False
        self.owner = None
        self.read_fd = None
        self.write_fd = None
        self.request = None
        self.start_body = b""
        self.target_pid = None
        self.original_write = SupervisedProcess._write_all
        spawn = supervisor._spawn_owner
        popen = process_module.subprocess.Popen

        def no_bytecode_owner(argv, **kwargs):
            # 仍由原 spawn 和原 Popen 启动，只为 Owner 解释器禁止写入字节码。
            if tuple(argv[:3]) == (sys.executable, "-m", "harnessix.processes.posix_owner"):
                argv = (argv[0], "-B", *argv[1:])
            return popen(argv, **kwargs)

        def observe_spawn(read_fd, run_directory):
            self.read_fd = read_fd
            self.owner = spawn(read_fd, run_directory)
            self.spawned.set()
            if block_spawn and not self.release.wait(_GATE_SECONDS):
                self.gate_timed_out = True
                raise TimeoutError("实际Owner创建后的有限交接门闩超时")
            return self.owner

        def write_then_gate(descriptor, body):
            self.original_write(descriptor, body)
            if self.entered.is_set():
                return
            self.request, _ = decode_owner_start(body.rstrip(b"\n"))
            self.write_fd, self.start_body = descriptor, body
            self.entered.set()
            try:
                if self.block_write and not self.release.wait(_GATE_SECONDS):
                    self.gate_timed_out = True
                    raise TimeoutError("原启动写入后的有限门闩超时")
            finally:
                self.returned.set()

        monkeypatch.setattr(process_module.subprocess, "Popen", no_bytecode_owner)
        monkeypatch.setattr(supervisor, "_spawn_owner", observe_spawn)
        monkeypatch.setattr(SupervisedProcess, "_write_all", staticmethod(write_then_gate))

    async def receipt(self, prepared):
        path = self.supervisor._runs / str(prepared.spec.process_id) / "receipt.json"
        async with asyncio.timeout(_WAIT_SECONDS):
            while True:
                lease = self.supervisor.status(prepared.spec.process_id)
                try:
                    receipt = await asyncio.to_thread(
                        read_owner_receipt,
                        path,
                        owner_token=lease.owner_token,
                        process_id=lease.process_id,
                        owner_identity=self.request.owner_identity,
                    )
                except KernelError as error:
                    if error.code != "process_owner_receipt_missing":
                        raise
                    await asyncio.sleep(0.01)
                else:
                    self.target_pid = receipt.pid
                    return receipt

    async def cleanup(self, task, prepared):
        """不补登记、不推进 Lease；直接回收本夹具真实 Owner、目标和控制 FD。"""
        self.release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            async with asyncio.timeout(_WAIT_SECONDS):
                await asyncio.gather(task, return_exceptions=True)
        if self.entered.is_set():
            assert await asyncio.to_thread(self.returned.wait, _WAIT_SECONDS)
        handle = self.supervisor._handles.get(prepared.spec.process_id)
        try:
            if handle is not None:
                async with asyncio.timeout(_WAIT_SECONDS):
                    await handle.aclose()
            elif self.write_fd is not None and _fd_open(self.write_fd):
                command = ProcessOwnerCommand(operation="stop", reason="cancelled")
                self.original_write(
                    self.write_fd, command.model_dump_json().encode("utf-8") + b"\n"
                )
                os.close(self.write_fd)
            if self.owner is not None:
                await asyncio.to_thread(self.owner.wait, timeout=_WAIT_SECONDS)
        finally:
            # 只处理由本次原 spawn 返回的 Popen 和验签回执确认的单一短进程。
            if self.write_fd is not None and _fd_open(self.write_fd):
                os.close(self.write_fd)
            if self.owner is not None and self.owner.poll() is None:
                if self.target_pid is not None:
                    try:
                        os.killpg(self.target_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.owner.kill()
                await asyncio.to_thread(self.owner.wait, timeout=_WAIT_SECONDS)
        return {
            "gate_released": self.release.is_set(),
            "write_thread_returned": self.returned.is_set(),
            "gate_timed_out": self.gate_timed_out,
            "control_fd_closed": self.write_fd is None or not _fd_open(self.write_fd),
            "read_fd_closed": self.read_fd is None or not _fd_open(self.read_fd),
            "owner_returncode": None if self.owner is None else self.owner.returncode,
            "owner_alive": self.owner is not None and self.owner.poll() is None,
            "target_alive": self.target_pid is not None
            and original._process_running(self.target_pid),
        }


@asynccontextmanager
async def _shared_case(make_process):
    # 原工厂提供临时 Runner/端口；短 Python 的 argv、环境和身份仍经正式合同冻结。
    case = make_process(python_code=_PROGRAM, executable=Path(sys.executable))
    case.runner.path = Path(sys.executable)
    case.runner._global = ("-B", "-I", "-c", _PROGRAM)
    case.runner._binding = replace(
        case.runner._binding,
        executable=case.runner.path,
        global_arguments=case.runner._global,
    )
    await case.port.aclose()
    protection = SecretPublicationScope((), {})
    try:
        with (
            product_state_owner(case.state) as owner,
            SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
        ):
            async with PosixProcessSupervisor(
                case.state / "process-owner", output_redaction=protection
            ) as supervisor:
                host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
                case.port = GitDeliveryProcess(
                    case.runner, case.state, output_redaction=protection, runtime_host=host
                )
                prepared = case.port.prepare(case.workspace, (), budget=GitOperationBudget(10))
                # 原命令端口的正常Plan/批准，不签发认证父Session或Router批准。
                plan = original._plan(case, prepared)
                checkpoint = original._checkpoint(plan)
                assert execution_is_approved(plan, checkpoint)
                save_git_process_plan(case.state, plan, host)
                yield case, host, prepared, plan, checkpoint
    finally:
        protection.close()


def _observation(host, prepared, window):
    lease = host.supervisor.status(prepared.spec.process_id)
    return {
        "process_id": str(lease.process_id),
        "lease_state": lease.state,
        "lease_sequence": lease.sequence,
        "stop_reason": lease.stop_reason,
        "active_count": len(host.supervisor.active_leases()),
        "registered": prepared.spec.process_id in host.supervisor._handles,
        "control_fd_open": _fd_open(window.write_fd),
        "owner_pid": window.owner.pid,
        "owner_alive": window.owner.poll() is None,
        "target_pid": window.target_pid,
        "target_alive": original._process_running(window.target_pid),
    }


def _evidence(name, host, prepared, plan, checkpoint, window, observation, cleanup):
    facts = {"case": name, "after_cancellation": observation, "fixture_cleanup": cleanup}
    print("PRE_HANDOFF_EVIDENCE " + json.dumps(facts, sort_keys=True))
    destination = os.environ.get("HARNESSIX_PRE_HANDOFF_EVIDENCE_ROOT")
    if not destination:
        return
    root = Path(destination) / name
    root.mkdir(mode=0o700, exist_ok=False)
    records = {
        "observation.json": json.dumps(facts, indent=2).encode(),
        "plan.json": plan.model_dump_json().encode(),
        "checkpoint.json": checkpoint.model_dump_json().encode(),
        "spec.json": prepared.spec.model_dump_json().encode(),
        "start-control.bin": window.start_body,
        "lease-after-fixture-cleanup.json": host.supervisor.status(prepared.spec.process_id)
        .model_dump_json()
        .encode(),
    }
    receipt = host.supervisor._runs / str(prepared.spec.process_id) / "receipt.json"
    if receipt.exists():
        records["receipt-after-fixture-cleanup.json"] = receipt.read_bytes()
    records["start-control.sha256"] = hashlib.sha256(window.start_body).hexdigest().encode()
    for filename, body in records.items():
        path = root / filename
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
    with sqlite3.connect(root / "process-leases.db") as frozen:
        host.supervisor._store._db.backup(frozen)
    (root / "process-leases.db").chmod(0o600)


@pytest.mark.parametrize("stage", ["spawn", "write"])
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_start_cancel_before_handle_registration(
    make_process, monkeypatch, stage: str, cancel_count: int
):
    async with _shared_case(make_process) as (case, host, prepared, plan, checkpoint):
        window = _RealStartWindow(
            host.supervisor, monkeypatch, block_write=stage == "write", block_spawn=stage == "spawn"
        )
        task = asyncio.create_task(start_git_process(host.supervisor, prepared, plan, checkpoint))
        observation = {}
        try:
            reached = window.entered if stage == "write" else window.spawned
            assert await asyncio.to_thread(reached.wait, _WAIT_SECONDS)
            if stage == "write":
                receipt = await window.receipt(prepared)
                assert receipt.state == "running", "真实Owner必须先产生可验签运行见证"
            assert prepared.spec.process_id not in host.supervisor._handles
            assert not window.returned.is_set(), "取消必须位于真实写入后、线程返回前"
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "交接未完成时取消不得使父调用提前返回"
            window.release.set()
            async with asyncio.timeout(_WAIT_SECONDS):
                with pytest.raises(asyncio.CancelledError):
                    await task
            window.target_pid = host.supervisor.status(prepared.spec.process_id).pid
            observation = _observation(host, prepared, window)
            host.checkpoint(case.state)
        finally:
            cleanup = await window.cleanup(task, prepared)
            _evidence(
                f"pre_handoff_{stage}_{cancel_count}",
                host,
                prepared,
                plan,
                checkpoint,
                window,
                observation,
                cleanup,
            )
        assert not cleanup["gate_timed_out"]
        assert cleanup["control_fd_closed"] and cleanup["read_fd_closed"]
        assert not cleanup["owner_alive"] and not cleanup["target_alive"]
        # 安全性质断言使用夹具清理前的真实事实，不能用事后手工回收把 RED 洗成 GREEN。
        assert observation["active_count"] == 0, observation
        assert observation["lease_state"] in {"exited", "failed"}, observation
        assert not observation["control_fd_open"], observation
        assert not observation["owner_alive"] and not observation["target_alive"], observation


async def test_start_cancel_after_registered_running_receipt_settles(make_process, monkeypatch):
    async with _shared_case(make_process) as (case, host, prepared, plan, checkpoint):
        window = _RealStartWindow(host.supervisor, monkeypatch, block_write=False)
        reached, release = asyncio.Event(), asyncio.Event()
        refresh = SupervisedProcess.refresh

        async def refresh_then_gate(handle):
            result = await refresh(handle)
            if result.process_id == prepared.spec.process_id and result.state == "running":
                if not reached.is_set():
                    window.target_pid = result.pid
                    reached.set()
                    await asyncio.wait_for(release.wait(), _GATE_SECONDS)
            return result

        monkeypatch.setattr(SupervisedProcess, "refresh", refresh_then_gate)
        task = asyncio.create_task(start_git_process(host.supervisor, prepared, plan, checkpoint))
        observation = {}
        try:
            async with asyncio.timeout(_WAIT_SECONDS):
                await reached.wait()
            assert prepared.spec.process_id in host.supervisor._handles
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "取消须等待本次启动交接，而不能取消原启动任务"
            release.set()
            async with asyncio.timeout(_WAIT_SECONDS):
                with pytest.raises(asyncio.CancelledError):
                    await task
            observation = _observation(host, prepared, window)
            host.checkpoint(case.state)
            assert not host.supervisor._closed and not host.plans._closed
        finally:
            release.set()
            cleanup = await window.cleanup(task, prepared)
            _evidence(
                "registered_handoff",
                host,
                prepared,
                plan,
                checkpoint,
                window,
                observation,
                cleanup,
            )
        assert observation["active_count"] == 0, observation
        assert observation["lease_state"] == "exited", observation
        assert observation["stop_reason"] == "cancelled", observation
        assert not observation["control_fd_open"], observation
        assert not observation["owner_alive"] and not observation["target_alive"], observation
        assert cleanup["control_fd_closed"] and cleanup["read_fd_closed"]
        assert not cleanup["owner_alive"] and not cleanup["target_alive"]
