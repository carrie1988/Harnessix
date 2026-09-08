from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    verify_execution_plan_v2,
)
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.sandbox.capabilities import probe_container_engine
from harnessix.sandbox.container import ContainerCommandBuilder, ManagedEgressBinding
from harnessix.sandbox.contracts import NetworkDestination, NetworkPolicy
from harnessix.sandbox.egress import EgressGatewayLimits, egress_gateway_digest
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.planner import build_container_command, build_container_sandbox_profile
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _runner(argv, timeout):
    assert timeout == 5.0
    assert argv[1:3] == ("version", "--format")
    return subprocess.CompletedProcess(argv, 0, "28.3.2|28.3.2|[name=seccomp]\n", "")


def _fixtures(root: Path, mode="none"):
    engine = Path(sys.executable)
    probe = probe_container_engine(engine, engine="docker", runner=_runner)
    if mode in {"limited", "restricted"}:
        policy = NetworkPolicy(
            mode=mode,
            destinations=(
                NetworkDestination(
                    kind="domain",
                    value="packages.example.com",
                    protocol="https",
                    ports=(443,),
                ),
            ),
        )
        network = resolve_network_policy(policy, resolver=lambda _: ("93.184.216.34",), now=NOW)
    else:
        network = resolve_network_policy(NetworkPolicy(mode=mode), now=NOW)
    gateway_digest = (
        egress_gateway_digest(network, EgressGatewayLimits())
        if mode in {"limited", "restricted"}
        else None
    )
    profile = build_container_sandbox_profile(
        image="sha256:" + "a" * 64,
        workspace_mode="read_write",
        network=network,
        egress_gateway_digest=gateway_digest,
    )
    snapshot = capture_workspace_snapshot(
        root,
        resources=(
            WorkspaceResourceRequest(path=".", access="write"),
            WorkspaceResourceRequest(path="main.py", access="write"),
        ),
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
    sandbox = SandboxBindingV2(
        level="container_strong",
        backend="docker",
        backend_version=probe.server_version,
        network=mode,
        capability_digest=capabilities.evidence_digest,
        profile_digest=profile.digest,
    )
    command = build_container_command(("python", "-V"), profile_digest=profile.digest)
    intent = ExecutionIntent(
        source="builtin",
        source_id="harnessix",
        tool="sandbox.process",
        tool_version="v1",
        tool_fingerprint="b" * 64,
        arguments=command.model_dump(mode="json", warnings="error"),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        idempotency_key="turn/call",
    )
    policy_binding = ExecutionPolicyBinding(
        version="sandbox/v1",
        decision=PolicyDecisionKind.ALLOW,
        policy_id="container.default",
        reason_code="container_isolated",
    )
    secret_bindings = (SecretVersionBinding(name="registry", version="7", target="TOKEN"),)
    plan = build_execution_plan_v2(
        intent,
        snapshot,
        environment={"LANG": "C"},
        secrets=secret_bindings,
        sandbox=sandbox,
        policy=policy_binding,
        capabilities=capabilities,
    )
    return engine, probe, profile, command, plan


def test_container_argv_is_fixed_and_never_contains_secret_value(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("before", encoding="utf-8")
    engine, probe, profile, command, plan = _fixtures(tmp_path)
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("registry", "7", "HOST_TOKEN"),),
        environment={"HOST_TOKEN": "secret-canary-value"},
    )
    with resolve_secret_environment(
        plan.secrets, provider, platform=plan.workspace.platform
    ) as secret:
        verify_execution_plan_v2(
            plan,
            intent=plan.intent,
            workspace=plan.workspace,
            environment={"LANG": "C"},
            secrets=plan.secrets,
            sandbox=plan.sandbox,
            policy=plan.policy,
            capabilities=plan.capabilities,
        )
        launch = ContainerCommandBuilder(engine, probe).prepare(
            plan,
            None,
            profile,
            workspace=tmp_path,
            command=command,
            environment={"LANG": "C"},
            secrets=secret,
        )
        assert launch.materialize_environment()["TOKEN"] == "secret-canary-value"
        assert launch.redaction_values() == (b"secret-canary-value",)
    joined = "\0".join(launch.argv)
    assert "secret-canary-value" not in joined
    assert "secret-canary-value" not in repr(launch)
    with pytest.raises(KernelError) as closed:
        launch.materialize_environment()
    assert closed.value.code == "secret_scope_closed"
    for required in (
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--network",
        "none",
        "--pull",
        "never",
    ):
        assert required in launch.argv
    assert launch.argv[-4:] == ("--entrypoint", "python", profile.image, "-V")


def test_container_rechecks_workspace_environment_profile_and_approval(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("before", encoding="utf-8")
    engine, probe, profile, command, plan = _fixtures(tmp_path)
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("registry", "7", "HOST_TOKEN"),),
        environment={"HOST_TOKEN": "secret-canary-value"},
    )
    with resolve_secret_environment(
        plan.secrets, provider, platform=plan.workspace.platform
    ) as secret:
        changed_command = build_container_command(("python", "-VV"), profile_digest=profile.digest)
        with pytest.raises(KernelError) as changed:
            ContainerCommandBuilder(engine, probe).prepare(
                plan,
                None,
                profile,
                workspace=tmp_path,
                command=changed_command,
                environment={"LANG": "C"},
                secrets=secret,
            )
        assert changed.value.code == "sandbox_capability_mismatch"

        with pytest.raises(KernelError) as environment:
            ContainerCommandBuilder(engine, probe).prepare(
                plan,
                None,
                profile,
                workspace=tmp_path,
                command=command,
                environment={"LANG": "en_US.UTF-8"},
                secrets=secret,
            )
        assert environment.value.code == "execution_plan_stale"

        target.write_text("after", encoding="utf-8")
        with pytest.raises(KernelError) as workspace:
            ContainerCommandBuilder(engine, probe).prepare(
                plan,
                None,
                profile,
                workspace=tmp_path,
                command=command,
                environment={"LANG": "C"},
                secrets=secret,
            )
        assert workspace.value.code == "execution_plan_stale"


def test_selective_network_fails_without_matching_internal_gateway(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("before", encoding="utf-8")
    engine, probe, profile, command, plan = _fixtures(tmp_path, mode="limited")
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("registry", "7", "HOST_TOKEN"),),
        environment={"HOST_TOKEN": "secret-canary-value"},
    )
    with resolve_secret_environment(
        plan.secrets, provider, platform=plan.workspace.platform
    ) as secret:
        with pytest.raises(KernelError) as missing:
            ContainerCommandBuilder(engine, probe).prepare(
                plan,
                None,
                profile,
                workspace=tmp_path,
                command=command,
                environment={"LANG": "C"},
                secrets=secret,
            )
        assert missing.value.code == "network_policy_unenforceable"

        binding = ManagedEgressBinding(
            network_name="harnessix-internal-123456789abc",
            proxy_url="http://harnessix-egress:8080",
            policy_digest=profile.network.digest,
            gateway_digest=profile.egress_gateway_digest,
            internal_network_attestation="d" * 64,
        )
        launch = ContainerCommandBuilder(engine, probe).prepare(
            plan,
            None,
            profile,
            workspace=tmp_path,
            command=command,
            environment={"LANG": "C"},
            secrets=secret,
            egress=binding,
        )
        assert launch.materialize_environment()["HTTPS_PROXY"] == binding.proxy_url
        assert launch.materialize_environment()["NO_PROXY"] == ""
    assert binding.network_name in launch.argv


def test_execution_plan_v2_round_trips_through_durable_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("before", encoding="utf-8")
    _, _, _, _, plan = _fixtures(workspace)
    database = tmp_path / "private/execution.db"
    with SQLiteExecutionPlanStore(database) as store:
        store.save_plan(plan)
    with SQLiteExecutionPlanStore(database) as reopened:
        loaded = reopened.load_plan(plan.plan_id)
    assert isinstance(loaded, ExecutionPlanV2)
    assert loaded == plan
