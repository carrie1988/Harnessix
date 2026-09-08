from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    SandboxBindingV2,
)
from harnessix.execution.planner import build_capability_evidence_v2, build_execution_plan_v2
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.sandbox.capabilities import ContainerEngineProbe, probe_container_engine
from harnessix.sandbox.container import ContainerCommandBuilder
from harnessix.sandbox.contracts import (
    ContainerExecutionSpec,
    ContainerSandboxProfile,
    ManagedEgressBinding,
    NetworkDestination,
    NetworkPolicy,
)
from harnessix.sandbox.egress import EgressGatewayLimits, egress_gateway_digest
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.network_isolation import (
    GATEWAY_LABEL,
    POLICY_LABEL,
    attest_internal_network,
)
from harnessix.sandbox.planner import (
    build_container_command,
    build_container_execution,
    build_container_sandbox_profile,
)
from harnessix.sandbox.process_runtime import ContainerProcessRuntime
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot


def _probe_runner(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    assert timeout == 15.0
    output = "28.3.2|28.3.2\n" if argv[1] == "version" else '["name=seccomp"]\n'
    return subprocess.CompletedProcess(argv, 0, output, "")


def _execution_plan(
    root: Path,
    supervisor: PosixProcessSupervisor,
    *,
    mode: Literal["none", "limited"] = "none",
) -> tuple[
    tuple[ContainerEngineProbe, ContainerSandboxProfile],
    ContainerExecutionSpec,
    ExecutionPlanV2,
]:
    probe = probe_container_engine(Path(sys.executable), engine="docker", runner=_probe_runner)
    if mode == "limited":
        policy = NetworkPolicy(
            mode="limited",
            destinations=(
                NetworkDestination(
                    kind="domain",
                    value="packages.example.com",
                    protocol="https",
                    ports=(443,),
                ),
            ),
        )
        network = resolve_network_policy(
            policy,
            resolver=lambda _: ("93.184.216.34",),
            now=datetime(2026, 9, 8, tzinfo=UTC),
        )
        gateway_digest = egress_gateway_digest(network, EgressGatewayLimits())
    else:
        network = resolve_network_policy(
            NetworkPolicy(mode="none"), now=datetime(2026, 9, 8, tzinfo=UTC)
        )
        gateway_digest = None
    profile = build_container_sandbox_profile(
        image="sha256:" + "a" * 64,
        workspace_mode="read_write",
        network=network,
        egress_gateway_digest=gateway_digest,
    )
    command = build_container_command(("/bin/sh", "-c", "cat"), profile_digest=profile.digest)
    process = build_process_spec(
        invocation="argv",
        argv=command.argv,
        stdin="pipe",
        lifecycle="background",
        timeout_seconds=17,
        output_bytes=8192,
        input_bytes=1024,
    )
    execution = build_container_execution(
        command,
        process,
        owner_capability_digest=supervisor.capability.digest,
    )
    snapshot = capture_workspace_snapshot(
        root, resources=(WorkspaceResourceRequest(path=".", access="write"),)
    )
    capabilities = build_capability_evidence_v2(
        platform=snapshot.platform,
        provider="docker",
        provider_version=probe.server_version,
        sandbox_levels=("container_strong",),
        network_modes=(mode,),
        supports_pty=False,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=probe.digest,
    )
    plan = build_execution_plan_v2(
        ExecutionIntent(
            source="builtin",
            source_id="harnessix",
            tool="sandbox.process",
            tool_version="v1",
            tool_fingerprint="a" * 64,
            arguments=execution.model_dump(mode="json", warnings="error"),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            idempotency_key=str(process.process_id),
        ),
        snapshot,
        environment={"LANG": "C"},
        secrets=(),
        sandbox=SandboxBindingV2(
            level="container_strong",
            backend="docker",
            backend_version=probe.server_version,
            network=mode,
            capability_digest=capabilities.evidence_digest,
            profile_digest=profile.digest,
        ),
        policy=ExecutionPolicyBinding(
            version="sandbox/v1",
            decision=PolicyDecisionKind.ALLOW,
            policy_id="container.test",
            reason_code="test",
        ),
        capabilities=capabilities,
    )
    return (probe, profile), execution, plan


@pytest.mark.skipif(sys.platform == "win32", reason="该确定性用例使用POSIX owner能力")
async def test_container_execution_materializes_exact_supervised_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, profile), execution, plan = _execution_plan(workspace, supervisor)
        runtime = ContainerProcessRuntime(
            ContainerCommandBuilder(Path(sys.executable), probe), supervisor
        )
        prepared = runtime.prepare(
            plan,
            None,
            profile,
            execution,
            workspace=workspace,
            environment={"LANG": "C"},
        )
    assert prepared.binding.kind == "container"
    assert prepared.binding.plan_fingerprint == plan.fingerprint
    assert prepared.process.process_id == execution.process.process_id
    assert prepared.process.lifecycle == "background"
    assert prepared.process.timeout_seconds == 17
    assert prepared.process.stdin == "pipe" and prepared.process.input_bytes == 1024
    assert "--interactive" in prepared.process.argv
    assert ContainerCommandBuilder.container_name(execution) in prepared.process.argv
    assert f"com.harnessix.execution-spec={execution.digest}" in prepared.process.argv
    assert prepared.binding.environment[0].name == "LANG"


@pytest.mark.skipif(sys.platform == "win32", reason="该确定性用例使用POSIX owner能力")
async def test_container_execution_rejects_owner_or_plan_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, profile), execution, plan = _execution_plan(workspace, supervisor)
        runtime = ContainerProcessRuntime(
            ContainerCommandBuilder(Path(sys.executable), probe), supervisor
        )
        changed_owner = execution.model_copy(update={"owner_capability_digest": "f" * 64})
        with pytest.raises(KernelError) as owner:
            runtime.prepare(
                plan,
                None,
                profile,
                changed_owner,
                workspace=workspace,
                environment={"LANG": "C"},
            )
        assert owner.value.code == "process_capability_mismatch"

        changed_process = build_process_spec(
            invocation="argv",
            argv=execution.command.argv,
            timeout_seconds=18,
            process_id=execution.process.process_id,
        )
        changed_execution = build_container_execution(
            execution.command,
            changed_process,
            owner_capability_digest=supervisor.capability.digest,
        )
        with pytest.raises(KernelError) as plan_drift:
            runtime.prepare(
                plan,
                None,
                profile,
                changed_execution,
                workspace=workspace,
                environment={"LANG": "C"},
            )
        assert plan_drift.value.code == "sandbox_capability_mismatch"


@pytest.mark.skipif(sys.platform == "win32", reason="该确定性用例使用POSIX owner能力")
async def test_container_selective_network_is_reattested_immediately(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, profile), execution, plan = _execution_plan(workspace, supervisor, mode="limited")
        name = "harnessix-internal-123456789abc"
        document: list[dict[str, Any]] = [
            {
                "Name": name,
                "Internal": True,
                "Driver": "bridge",
                "Labels": {
                    POLICY_LABEL: profile.network.digest,
                    GATEWAY_LABEL: profile.egress_gateway_digest,
                },
                "Containers": {"gateway": {"Name": "harnessix-egress"}},
            }
        ]
        body = json.dumps(document).encode()
        assert profile.egress_gateway_digest is not None
        attested = attest_internal_network(
            body,
            network_name=name,
            proxy_port=8080,
            policy=profile.network,
            gateway_digest=profile.egress_gateway_digest,
        )
        binding = ManagedEgressBinding(
            network_name=name,
            proxy_url="http://harnessix-egress:8080",
            policy_digest=profile.network.digest,
            gateway_digest=profile.egress_gateway_digest,
            internal_network_attestation=attested.internal_network_attestation,
        )
        calls: list[tuple[str, ...]] = []

        def inspect(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
            calls.append(tuple(argv))
            assert timeout == 5.0
            return subprocess.CompletedProcess(argv, 0, body, b"")

        runtime = ContainerProcessRuntime(
            ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=inspect),
            supervisor,
        )
        prepared = runtime.prepare(
            plan,
            None,
            profile,
            execution,
            workspace=workspace,
            environment={"LANG": "C"},
            egress=binding,
        )
        document[0]["Containers"]["untrusted"] = {"Name": "untrusted"}
        changed_body = json.dumps(document).encode()

        def changed_inspect(
            argv: Sequence[str], timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 0, changed_body, b"")

        changed_runtime = ContainerProcessRuntime(
            ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=changed_inspect),
            supervisor,
        )
        with pytest.raises(KernelError) as changed:
            changed_runtime.prepare(
                plan,
                None,
                profile,
                execution,
                workspace=workspace,
                environment={"LANG": "C"},
                egress=binding,
            )
    assert len(calls) == 1 and calls[0][1:] == ("network", "inspect", name)
    assert binding.network_name in prepared.process.argv
    assert changed.value.code == "network_policy_unenforceable"


@pytest.mark.skipif(sys.platform == "win32", reason="该确定性用例使用POSIX owner能力")
async def test_container_cleanup_uses_bound_labels_and_verifies_absence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, _), execution, _ = _execution_plan(workspace, supervisor)
        name = ContainerCommandBuilder.container_name(execution)
        row = f"0123456789ab|{name}|{execution.process.process_id}|{execution.digest}\n".encode()
        list_outputs = [row, b""]
        calls: list[tuple[str, ...]] = []

        def control(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
            calls.append(tuple(argv))
            if tuple(argv[1:4]) == ("container", "rm", "--force"):
                assert timeout == 10.0 and argv[4] == "0123456789ab"
                return subprocess.CompletedProcess(argv, 0, b"0123456789ab\n", b"")
            assert tuple(argv[1:3]) == ("container", "ls") and timeout == 5.0
            return subprocess.CompletedProcess(argv, 0, list_outputs.pop(0), b"")

        builder = ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=control)
        result = await asyncio.to_thread(builder.cleanup_container, execution)
    assert result == "removed"
    assert len(calls) == 3 and not list_outputs


@pytest.mark.skipif(sys.platform == "win32", reason="该确定性用例使用POSIX owner能力")
async def test_container_cleanup_rejects_spoofed_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, _), execution, _ = _execution_plan(workspace, supervisor)
        spoofed = (
            f"0123456789ab|{ContainerCommandBuilder.container_name(execution)}|"
            f"{execution.process.process_id}|{'f' * 64}\n"
        ).encode()

        def control(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 0, spoofed, b"")

        builder = ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=control)
        with pytest.raises(KernelError) as invalid:
            await asyncio.to_thread(builder.cleanup_container, execution)
    assert invalid.value.code == "process_cleanup_failed"


@pytest.mark.skipif(sys.platform == "win32", reason="该确定性用例使用POSIX owner能力")
async def test_container_start_failure_still_verifies_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, profile), execution, plan = _execution_plan(workspace, supervisor)
        calls: list[tuple[str, ...]] = []

        def control(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
            calls.append(tuple(argv))
            assert tuple(argv[1:3]) == ("container", "ls") and timeout == 5.0
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        async def fail_start(*args: object, **kwargs: object) -> None:
            raise KernelError("process_launch_failed", "受控启动失败")

        monkeypatch.setattr(supervisor, "start_prepared", fail_start)
        runtime = ContainerProcessRuntime(
            ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=control),
            supervisor,
        )
        with pytest.raises(KernelError) as failed:
            await runtime.start(
                plan,
                None,
                profile,
                execution,
                workspace=workspace,
                environment={"LANG": "C"},
            )
    assert failed.value.code == "process_launch_failed"
    assert len(calls) == 2
