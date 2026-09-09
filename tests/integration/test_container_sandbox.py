from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPolicyBinding,
    SandboxBindingV2,
    SecretVersionBinding,
)
from harnessix.execution.planner import build_capability_evidence_v2, build_execution_plan_v2
from harnessix.mcp import McpClientConnection, McpContainerStdioTarget, SQLiteMcpStore
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.sandbox.capabilities import probe_container_engine
from harnessix.sandbox.container import ContainerCommandBuilder
from harnessix.sandbox.contracts import NetworkPolicy, SandboxResourceLimits
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.planner import (
    build_container_command,
    build_container_execution,
    build_container_sandbox_profile,
)
from harnessix.sandbox.process_runtime import ContainerProcessRuntime
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot


async def test_real_container_enforces_read_only_no_network_limits_and_secret_boundary(
    tmp_path: Path,
) -> None:
    image = os.environ.get("HARNESSIX_TEST_CONTAINER_IMAGE")
    docker = shutil.which("docker")
    if not image or not docker:
        pytest.skip("未配置固定摘要的真实Container Sandbox验收")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    target = workspace / "main.txt"
    target.write_text("workspace-content\n", encoding="utf-8")
    target.chmod(0o644)
    probe = probe_container_engine(Path(docker), engine="docker")
    network = resolve_network_policy(NetworkPolicy(mode="none"), now=datetime.now(UTC))
    profile = build_container_sandbox_profile(
        image=image,
        workspace_mode="read_only",
        network=network,
        limits=SandboxResourceLimits(
            cpus=0.5,
            memory_bytes=64 * 1024 * 1024,
            pids=16,
            tmpfs_bytes=16 * 1024 * 1024,
        ),
    )
    command = build_container_command(
        (
            "/bin/sh",
            "-c",
            'set -eu; test "$(id -u)" = 65532; '
            'test "$(ls /sys/class/net)" = lo; '
            "test \"$(awk '/CapEff/{print $2}' /proc/self/status)\" = 0000000000000000; "
            "cat /workspace/main.txt; touch /tmp/allowed; "
            "if touch /denied 2>/dev/null; then exit 21; fi; "
            "if echo changed >>/workspace/main.txt 2>/dev/null; then exit 22; fi; "
            "if [ -f /sys/fs/cgroup/pids.max ]; then "
            'test "$(cat /sys/fs/cgroup/pids.max)" = 16; fi; '
            "if [ -f /sys/fs/cgroup/memory.max ]; then "
            'test "$(cat /sys/fs/cgroup/memory.max)" = 67108864; fi; '
            "if dd if=/dev/zero of=/tmp/overrun bs=1048576 count=32 2>/dev/null; "
            "then exit 23; fi; rm -f /tmp/overrun; echo resource-limit-enforced; "
            "printf '%s\\n' \"$TOKEN\"",
        ),
        profile_digest=profile.digest,
    )
    snapshot = capture_workspace_snapshot(
        workspace,
        resources=(
            WorkspaceResourceRequest(path=".", access="read"),
            WorkspaceResourceRequest(path="main.txt", access="read"),
        ),
    )
    capabilities = build_capability_evidence_v2(
        platform=snapshot.platform,
        provider="docker",
        provider_version=probe.server_version,
        sandbox_levels=("container_strong",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=probe.digest,
    )
    sandbox = SandboxBindingV2(
        level="container_strong",
        backend="docker",
        backend_version=probe.server_version,
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=profile.digest,
    )
    policy = ExecutionPolicyBinding(
        version="sandbox/v1",
        decision=PolicyDecisionKind.ALLOW,
        policy_id="container.smoke",
        reason_code="isolated_test",
    )
    secret_bindings = (SecretVersionBinding(name="canary", version="1", target="TOKEN"),)
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("canary", "1", "HOST_CANARY"),),
        environment={"HOST_CANARY": "container-secret-canary"},
    )
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        process = build_process_spec(
            invocation="argv",
            argv=command.argv,
            timeout_seconds=30,
            output_bytes=64 * 1024,
        )
        execution = build_container_execution(
            command,
            process,
            owner_capability_digest=supervisor.capability.digest,
        )
        intent = ExecutionIntent(
            source="builtin",
            source_id="harnessix",
            tool="sandbox.process",
            tool_version="v1",
            tool_fingerprint="a" * 64,
            arguments=execution.model_dump(mode="json", warnings="error"),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            idempotency_key="container-smoke",
        )
        plan = build_execution_plan_v2(
            intent,
            snapshot,
            environment={"LANG": "C"},
            secrets=secret_bindings,
            sandbox=sandbox,
            policy=policy,
            capabilities=capabilities,
        )
        runtime = ContainerProcessRuntime(ContainerCommandBuilder(Path(docker), probe), supervisor)
        with resolve_secret_environment(
            plan.secrets, provider, platform=plan.workspace.platform
        ) as secret:
            handle = await runtime.start(
                plan,
                None,
                profile,
                execution,
                workspace=workspace,
                environment={"LANG": "C"},
                secrets=secret,
            )
        assert "container-secret-canary" not in "\0".join(handle.launch_argv)
        lease = await handle.wait()
        output = await handle.output("stdout") + await handle.output("stderr")
    assert lease.returncode == 0 and lease.stop_reason == "exited"
    assert b"workspace-content" in output
    assert b"resource-limit-enforced" in output
    assert b"container-secret-canary" not in output
    assert b"[REDACTED]" in output
    assert target.read_text(encoding="utf-8") == "workspace-content\n"


async def test_real_container_runs_mcp_stdio_with_frozen_sandbox_binding(
    tmp_path: Path,
) -> None:
    image = os.environ.get("HARNESSIX_TEST_CONTAINER_IMAGE")
    docker = shutil.which("docker")
    if not image or not docker:
        pytest.skip("未配置固定摘要的真实Container MCP验收")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    fixture = Path(__file__).parents[1] / "mcp/fixtures/busybox_server.sh"
    server = workspace / "mcp-server.sh"
    server.write_bytes(fixture.read_bytes())
    server.chmod(0o644)

    probe = probe_container_engine(Path(docker), engine="docker")
    network = resolve_network_policy(NetworkPolicy(mode="none"), now=datetime.now(UTC))
    profile = build_container_sandbox_profile(
        image=image,
        workspace_mode="read_only",
        network=network,
        limits=SandboxResourceLimits(
            cpus=0.5,
            memory_bytes=64 * 1024 * 1024,
            pids=16,
            tmpfs_bytes=16 * 1024 * 1024,
        ),
    )
    command = build_container_command(
        ("/bin/sh", "/workspace/mcp-server.sh"), profile_digest=profile.digest
    )
    snapshot = capture_workspace_snapshot(
        workspace,
        resources=(
            WorkspaceResourceRequest(path=".", access="read"),
            WorkspaceResourceRequest(path="mcp-server.sh", access="read"),
        ),
    )
    capabilities = build_capability_evidence_v2(
        platform=snapshot.platform,
        provider="docker",
        provider_version=probe.server_version,
        sandbox_levels=("container_strong",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=probe.digest,
    )
    sandbox = SandboxBindingV2(
        level="container_strong",
        backend="docker",
        backend_version=probe.server_version,
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=profile.digest,
    )
    process = build_process_spec(
        invocation="argv",
        argv=command.argv,
        stdin="pipe",
        lifecycle="background",
        timeout_seconds=30,
        output_bytes=64 * 1024,
        input_bytes=64 * 1024,
    )
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        execution = build_container_execution(
            command,
            process,
            owner_capability_digest=supervisor.capability.digest,
        )
        plan = build_execution_plan_v2(
            ExecutionIntent(
                source="mcp",
                source_id="container-fixture",
                tool="mcp.server",
                tool_version="1",
                tool_fingerprint="c" * 64,
                arguments=execution.model_dump(mode="json", warnings="error"),
                effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
                risk_level=RiskLevel.HIGH,
                idempotency_key="container-mcp-fixture",
            ),
            snapshot,
            environment={"LANG": "C"},
            secrets=(),
            sandbox=sandbox,
            policy=ExecutionPolicyBinding(
                version="sandbox/v1",
                decision=PolicyDecisionKind.ALLOW,
                policy_id="container.mcp",
                reason_code="isolated_test",
            ),
            capabilities=capabilities,
        )
        builder = ContainerCommandBuilder(Path(docker), probe)
        launch = builder.prepare(
            plan,
            None,
            profile,
            workspace=workspace,
            command=execution,
            environment={"LANG": "C"},
        )
        target = McpContainerStdioTarget(
            server_id="container-fixture",
            prepared=launch,
            execution=execution,
            profile=profile,
            builder=builder,
            startup_timeout_seconds=10,
            call_timeout_seconds=5,
        )
        store = SQLiteMcpStore(tmp_path / "mcp.db")
        connection = await McpClientConnection.connect(target, store)
        selected = connection.tool("echo")
        output = await connection.call(
            expected_catalog_sha256=connection.catalog.catalog_sha256,
            expected_tool_sha256=selected.tool_sha256,
            raw_name="echo",
            arguments={"text": "Harnessix"},
        )
        assert output.structured_content == {"ok": True}
        await connection.aclose()
        assert store.load("container-fixture").state == "closed"
