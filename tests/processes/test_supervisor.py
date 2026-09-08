from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
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
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from tests.processes.helpers import stopped

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX Process owner语义")


def _plan(
    root: Path,
    spec: ProcessSpec,
    supervisor: PosixProcessSupervisor,
    *,
    environment: dict[str, str] | None = None,
    secrets: tuple[SecretVersionBinding, ...] = (),
) -> ExecutionPlanV2:
    snapshot = capture_workspace_snapshot(
        root, resources=(WorkspaceResourceRequest(path=".", access="write"),)
    )
    capability = supervisor.capability
    execution_capability = build_capability_evidence_v2(
        platform="posix",
        provider="posix_process_owner",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=capability.supports_pty,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=capability.digest,
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="full",
        capability_digest=execution_capability.evidence_digest,
        profile_digest="b" * 64,
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
        environment=environment or {},
        secrets=secrets,
        sandbox=sandbox,
        policy=ExecutionPolicyBinding(
            version="process/v1",
            decision=PolicyDecisionKind.ALLOW,
            policy_id="process.test",
            reason_code="test",
        ),
        capabilities=execution_capability,
    )


async def test_pipe_process_uses_exact_environment_and_redacts_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "supervised-secret-canary"
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(
                sys.executable,
                "-I",
                "-c",
                "import os,sys; print(os.environ['VISIBLE']); "
                "print(os.environ['TOKEN']); print(os.environ['TOKEN'], file=sys.stderr)",
            ),
            output_bytes=4096,
        )
        bindings = (SecretVersionBinding(name="token", version="1", target="TOKEN"),)
        plan = _plan(
            workspace,
            spec,
            supervisor,
            environment={"VISIBLE": "public"},
            secrets=bindings,
        )
        provider = EnvironmentSecretProvider(
            (EnvironmentSecretSource("token", "1", "HOST_TOKEN"),),
            environment={"HOST_TOKEN": secret},
        )
        with resolve_secret_environment(bindings, provider, platform="posix") as resolved:
            handle = await supervisor.start(
                plan,
                spec,
                supervisor.capability,
                workspace=workspace,
                environment={"VISIBLE": "public"},
                secrets=resolved,
            )
        lease = await handle.wait()
        stdout = await handle.output("stdout")
        stderr = await handle.output("stderr")
    assert lease.state == "exited" and lease.returncode == 0 and lease.stop_reason == "exited"
    assert stdout == b"public\n[REDACTED]\n"
    assert stderr == b"[REDACTED]\n"
    persisted = b"".join(path.read_bytes() for path in (tmp_path / "state").rglob("*.*"))
    assert secret.encode() not in persisted


async def test_pipe_stdin_and_pty_resize_are_explicit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(
                sys.executable,
                "-I",
                "-c",
                "import os,sys; value=sys.stdin.readline().strip(); "
                "print(os.get_terminal_size()); print(value)",
            ),
            terminal="pty",
            stdin="pipe",
            input_bytes=128,
            output_bytes=4096,
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
        await handle.send_stdin("你好\n".encode())
        await handle.close_stdin()
        lease = await handle.wait()
        output = await handle.output("stdout")
    assert lease.state == "exited" and lease.returncode == 0
    assert b"132" in output and b"43" in output
    assert "你好".encode() in output
    assert lease.stderr.eof and lease.stderr.observed_bytes == 0


async def test_output_limit_stops_tree_and_preserves_verified_prefix(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(
        tmp_path / "state", terminate_grace_seconds=0.05
    ) as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(sys.executable, "-I", "-c", "import os\nwhile True: os.write(1,b'x'*8192)"),
            output_bytes=1024,
        )
        plan = _plan(workspace, spec, supervisor)
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        lease = await handle.wait()
        output = await handle.output("stdout")
    assert lease.state == "exited" and lease.stop_reason == "output_limit"
    assert output == b"x" * 1024
    assert lease.stdout.observed_bytes > lease.stdout.persisted_bytes
    assert lease.stdout.truncated and lease.stdout.eof


async def test_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "child.pid"
    async with PosixProcessSupervisor(
        tmp_path / "state", terminate_grace_seconds=0.05
    ) as supervisor:
        code = (
            "import signal,subprocess,sys,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "p=subprocess.Popen([sys.executable,'-I','-c',"
            "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)']); "
            "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
        )
        spec = build_process_spec(
            invocation="argv",
            argv=(sys.executable, "-I", "-c", code, str(marker)),
            timeout_seconds=0.3,
        )
        plan = _plan(workspace, spec, supervisor)
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        lease = await handle.wait()
    child_pid = int(marker.read_text())
    assert lease.state == "exited" and lease.stop_reason == "timeout"
    assert lease.returncode == -signal.SIGKILL
    await stopped(child_pid)


async def test_cancel_token_and_close_are_durable_stop_reasons(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = PosixProcessSupervisor(tmp_path / "state", terminate_grace_seconds=0.05)
    spec = build_process_spec(
        invocation="argv",
        argv=(sys.executable, "-I", "-c", "import time; time.sleep(30)"),
        lifecycle="background",
    )
    plan = _plan(workspace, spec, supervisor)
    handle = await supervisor.start(
        plan,
        spec,
        supervisor.capability,
        workspace=workspace,
        environment={},
    )
    token = CancelToken()
    token.cancel()
    lease = await handle.wait(token)
    assert lease.state == "exited" and lease.stop_reason == "cancelled"
    await supervisor.aclose()


async def test_control_loss_is_reconciled_without_pid_authority(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    first = PosixProcessSupervisor(state, terminate_grace_seconds=0.05)
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
    assert target_pid is not None
    owner = handle._owner  # noqa: SLF001 - 模拟宿主进程突然消失
    handle._close_control()  # noqa: SLF001 - 不走优雅停止路径
    first._store.close()  # noqa: SLF001 - 模拟宿主持久连接消失
    first._closed = True  # noqa: SLF001 - 禁止fixture再关闭旧owner

    async with PosixProcessSupervisor(state, terminate_grace_seconds=0.05) as recovered:
        lease = await recovered.reconcile(spec.process_id)
    assert lease.state == "exited" and lease.stop_reason == "host_lost"
    assert owner is not None
    await asyncio.to_thread(owner.wait)
    await stopped(target_pid)


async def test_launch_failure_is_terminal_and_duplicate_is_not_replayed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(str(workspace / "missing-program"),),
        )
        plan = _plan(workspace, spec, supervisor)
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        assert handle.lease.state == "failed"
        assert handle.lease.stop_reason == "launch_failed" and handle.lease.pid is None
        with pytest.raises(KernelError) as duplicate:
            await supervisor.start(
                plan,
                spec,
                supervisor.capability,
                workspace=workspace,
                environment={},
            )
    assert duplicate.value.code == "process_already_exists"


async def test_input_budget_overrun_stops_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(
        tmp_path / "state", terminate_grace_seconds=0.05
    ) as supervisor:
        spec = build_process_spec(
            invocation="argv",
            argv=(sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"),
            stdin="pipe",
            input_bytes=4,
        )
        plan = _plan(workspace, spec, supervisor)
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        await handle.send_stdin(b"12345")
        lease = await handle.wait()
    assert lease.state == "exited" and lease.stop_reason == "input_limit"


async def test_output_artifact_tampering_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    async with PosixProcessSupervisor(state) as supervisor:
        spec = build_process_spec(
            invocation="posix_sh",
            shell_source="printf 'trusted-output'",
        )
        plan = _plan(workspace, spec, supervisor)
        handle = await supervisor.start(
            plan,
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        assert (await handle.wait()).state == "exited"
        (state / "runs" / str(spec.process_id) / "stdout.bin").write_bytes(b"forged-output")
        with pytest.raises(KernelError) as corrupt:
            await handle.output("stdout")
    assert corrupt.value.code == "process_output_corrupt"
