from __future__ import annotations

import os
import shutil
import subprocess
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
from harnessix.sandbox.capabilities import probe_container_engine
from harnessix.sandbox.container import ContainerCommandBuilder
from harnessix.sandbox.contracts import NetworkPolicy
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.planner import build_container_command, build_container_sandbox_profile
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.secrets.redaction import redact_bytes
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot


def test_real_container_enforces_read_only_no_network_limits_and_secret_boundary(
    tmp_path: Path,
) -> None:
    image = os.environ.get("HARNESSIX_TEST_CONTAINER_IMAGE")
    docker = shutil.which("docker")
    if not image or not docker:
        pytest.skip("未配置固定摘要的真实Container Sandbox验收")
    tmp_path.chmod(0o755)
    target = tmp_path / "main.txt"
    target.write_text("workspace-content\n", encoding="utf-8")
    target.chmod(0o644)
    probe = probe_container_engine(Path(docker), engine="docker")
    network = resolve_network_policy(NetworkPolicy(mode="none"), now=datetime.now(UTC))
    profile = build_container_sandbox_profile(
        image=image,
        workspace_mode="read_only",
        network=network,
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
            "printf '%s\\n' \"$TOKEN\"",
        ),
        profile_digest=profile.digest,
    )
    snapshot = capture_workspace_snapshot(
        tmp_path,
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
    intent = ExecutionIntent(
        source="builtin",
        source_id="harnessix",
        tool="sandbox.process",
        tool_version="v1",
        tool_fingerprint="a" * 64,
        arguments=command.model_dump(mode="json", warnings="error"),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        idempotency_key="container-smoke",
    )
    policy = ExecutionPolicyBinding(
        version="sandbox/v1",
        decision=PolicyDecisionKind.ALLOW,
        policy_id="container.smoke",
        reason_code="isolated_test",
    )
    secret_bindings = (SecretVersionBinding(name="canary", version="1", target="TOKEN"),)
    plan = build_execution_plan_v2(
        intent,
        snapshot,
        environment={"LANG": "C"},
        secrets=secret_bindings,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
    )
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("canary", "1", "HOST_CANARY"),),
        environment={"HOST_CANARY": "container-secret-canary"},
    )
    with resolve_secret_environment(
        plan.secrets, provider, platform=plan.workspace.platform
    ) as secret:
        launch = ContainerCommandBuilder(Path(docker), probe).prepare(
            plan,
            None,
            profile,
            workspace=tmp_path,
            command=command,
            environment={"LANG": "C"},
            secrets=secret,
        )
        completed = subprocess.run(
            launch.argv,
            env=launch.materialize_environment(),
            capture_output=True,
            check=False,
            timeout=30,
        )
        output = redact_bytes(completed.stdout + completed.stderr, launch.redaction_values())
    assert completed.returncode == 0
    assert b"workspace-content" in output
    assert b"container-secret-canary" not in output
    assert b"[REDACTED]" in output
    assert target.read_text(encoding="utf-8") == "workspace-content\n"
