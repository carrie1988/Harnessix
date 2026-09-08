from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    SandboxBindingV2,
    SecretVersionBinding,
)
from harnessix.execution.planner import (
    build_capability_evidence_v2,
    build_execution_plan_v2,
)
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import WindowsProcessSupervisor
from harnessix.processes.windows_job import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_SUSPENDED,
    WindowsJobObject,
)
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object语义")


def _plan(
    root: Path,
    spec: ProcessSpec,
    supervisor: WindowsProcessSupervisor,
    *,
    secrets: tuple[SecretVersionBinding, ...] = (),
) -> ExecutionPlanV2:
    snapshot = capture_workspace_snapshot(
        root,
        resources=(WorkspaceResourceRequest(path=".", access="write"),),
        platform="windows",
    )
    capability = supervisor.capability
    execution_capability = build_capability_evidence_v2(
        platform="windows",
        provider="windows_process_owner",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=capability.supports_pty,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=capability.digest,
    )
    return build_execution_plan_v2(
        ExecutionIntent(
            source="builtin",
            source_id="harnessix",
            tool="process.supervised",
            tool_version="v1",
            tool_fingerprint="c" * 64,
            arguments=spec.model_dump(mode="json", warnings="error"),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            idempotency_key=str(spec.process_id),
        ),
        snapshot,
        environment={},
        secrets=secrets,
        sandbox=SandboxBindingV2(
            level="host_guarded",
            backend="host",
            backend_version="1",
            network="full",
            capability_digest=execution_capability.evidence_digest,
            profile_digest="b" * 64,
        ),
        policy=ExecutionPolicyBinding(
            version="process/v1",
            decision=PolicyDecisionKind.ALLOW,
            policy_id="process.test",
            reason_code="test",
        ),
        capabilities=execution_capability,
    )


def _is_running(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return kernel32.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle(handle)


async def _wait_stopped(pid: int) -> None:
    for _ in range(250):
        if not _is_running(pid):
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("Windows测试进程仍在运行")


async def test_windows_job_owner_pipe_unicode_secret_and_exact_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "windows-secret-canary"
    async with WindowsProcessSupervisor(tmp_path / "state") as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(
                sys.executable,
                "-I",
                "-c",
                "import os,sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); "
                "print(os.environ['TOKEN'])",
                "TOKEN",
            ),
            stdin="pipe",
            input_bytes=128,
            output_bytes=4096,
        )
        bindings = (SecretVersionBinding(name="token", version="1", target="TOKEN"),)
        plan = _plan(workspace, spec, supervisor, secrets=bindings)
        provider = EnvironmentSecretProvider(
            (EnvironmentSecretSource("token", "1", "HOST_TOKEN"),),
            environment={"HOST_TOKEN": secret},
        )
        with resolve_secret_environment(bindings, provider, platform="windows") as resolved:
            handle = await supervisor.start(
                plan,
                spec,
                supervisor.capability,
                workspace=workspace,
                environment={},
                secrets=resolved,
            )
        await handle.send_stdin("你好\n".encode())
        await handle.close_stdin()
        lease = await handle.wait()
        output = await handle.output("stdout")
    assert lease.state == "exited" and lease.returncode == 0, output.decode(errors="replace")
    assert "你好".encode() in output and b"[REDACTED]" in output
    assert secret.encode() not in output


async def test_windows_timeout_terminates_complete_job_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "child.pid"
    async with WindowsProcessSupervisor(tmp_path / "state") as supervisor:
        code = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
            "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
        )
        spec = build_process_spec(
            invocation="argv",
            argv=(sys.executable, "-I", "-c", code, str(marker)),
            timeout_seconds=0.5,
        )
        plan = _plan(workspace, spec, supervisor)
        lease = await supervisor.run(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
    child_pid = int(marker.read_text())
    assert lease.state == "exited" and lease.stop_reason == "timeout"
    await _wait_stopped(child_pid)


async def test_windows_conpty_unicode_input_resize_and_tree_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with WindowsProcessSupervisor(tmp_path / "state") as supervisor:
        assert supervisor.capability.supports_pty
        spec = build_process_spec(
            invocation="argv",
            argv=(
                sys.executable,
                "-I",
                "-c",
                "import os; value=input(); size=os.get_terminal_size(); "
                "print(f'{size.columns}x{size.lines}:{value.encode().hex()}', flush=True)",
            ),
            terminal="pty",
            stdin="pipe",
            input_bytes=128,
            output_bytes=4096,
            columns=100,
            rows=30,
        )
        plan = _plan(workspace, spec, supervisor)
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        await handle.resize(132, 43)
        await handle.send_stdin("cafeé 汉字\n".encode())
        await handle.close_stdin()
        lease = await handle.wait()
        output = await handle.output("stdout")
    expected = "cafeé 汉字".encode().hex().encode()
    assert lease.state == "exited" and lease.returncode == 0, output.decode(errors="replace")
    assert b"132x43:" + expected in output
    assert lease.stderr.eof and lease.stderr.observed_bytes == 0


async def test_windows_owner_loss_closes_job_and_restart_reconciles(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    first = WindowsProcessSupervisor(state)
    spec = build_process_spec(
        invocation="argv",
        argv=(sys.executable, "-I", "-c", "import time; time.sleep(30)"),
        lifecycle="background",
    )
    plan = _plan(workspace, spec, first)
    handle = await first.start(
        plan,
        spec,
        first.capability,
        workspace=workspace,
        environment={},
    )
    target_pid = handle.lease.pid
    owner = handle._owner  # noqa: SLF001 - 模拟宿主突然消失
    assert target_pid is not None and owner is not None
    handle._close_control()  # noqa: SLF001 - 不走优雅停止
    first._store.close()  # noqa: SLF001 - 模拟宿主持久连接断开
    first._closed = True  # noqa: SLF001
    async with WindowsProcessSupervisor(state) as recovered:
        lease = await recovered.reconcile(spec.process_id)
    assert lease.state == "exited" and lease.stop_reason == "host_lost"
    await asyncio.to_thread(owner.wait)
    await _wait_stopped(target_pid)


def test_windows_job_assigns_suspended_process_before_resume() -> None:
    process = subprocess.Popen(
        (sys.executable, "-I", "-c", "import time; time.sleep(30)"),
        creationflags=(CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP),
    )
    try:
        with WindowsJobObject() as job:
            job.assign_suspended(process.pid)
            assert job.contains(process.pid)
            job.terminate()
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
