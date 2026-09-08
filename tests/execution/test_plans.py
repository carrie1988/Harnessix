from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import (
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    PolicyDecisionKind,
    RiskLevel,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionIntent,
    ExecutionPlan,
    ExecutionPolicyBinding,
    SandboxBinding,
    SecretVersionBinding,
    execution_is_approved,
)
from harnessix.execution.planner import (
    bind_environment,
    build_capability_evidence,
    build_execution_plan,
    verify_execution_plan,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot


def fixtures(root: Path):
    snapshot = capture_workspace_snapshot(
        root,
        resources=(WorkspaceResourceRequest(path="main.py", access="write"),),
    )
    capabilities = build_capability_evidence(
        platform=snapshot.platform,
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
    )
    evidence = capabilities.evidence_digest
    sandbox = SandboxBinding(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="full",
        capability_digest=evidence,
    )
    intent = ExecutionIntent(
        source="builtin",
        source_id="harnessix",
        tool="host.process",
        tool_version="v2",
        tool_fingerprint="1" * 64,
        arguments={"argv": ["python", "-V"]},
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        idempotency_key="turn/call",
    )
    policy = ExecutionPolicyBinding(
        version="trusted-execution/v1",
        decision=PolicyDecisionKind.REQUIRE_APPROVAL,
        policy_id="default.host-write",
        reason_code="host_write",
    )
    secrets = (SecretVersionBinding(name="registry", version="7", target="TOKEN"),)
    return snapshot, capabilities, sandbox, intent, policy, secrets


def test_plan_binds_every_execution_fact_without_secret_plaintext(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("before", encoding="utf-8")
    snapshot, capabilities, sandbox, intent, policy, secrets = fixtures(tmp_path)
    plan = build_execution_plan(
        intent,
        snapshot,
        environment={"PATH": "/usr/bin"},
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
        plan_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    verify_execution_plan(
        plan,
        intent=intent,
        workspace=snapshot,
        environment={"PATH": "/usr/bin"},
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
    )
    wire = plan.model_dump_json()
    assert "/usr/bin" not in wire
    assert "secret-value" not in wire
    assert ExecutionPlan.model_validate_json(wire) == plan
    approval = ExecutionApprovalCheckpoint(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        decision=ApprovalRecord(
            outcome=ApprovalOutcome.APPROVED,
            actor="reviewer",
            request_fingerprint=plan.fingerprint,
        ),
    )
    assert execution_is_approved(plan, approval)
    assert not execution_is_approved(plan, None)


def test_plan_rejects_argument_environment_workspace_policy_and_capability_drift(
    tmp_path: Path,
) -> None:
    target = tmp_path / "main.py"
    target.write_text("before", encoding="utf-8")
    snapshot, capabilities, sandbox, intent, policy, secrets = fixtures(tmp_path)
    plan = build_execution_plan(
        intent,
        snapshot,
        environment={"PATH": "/usr/bin"},
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
    )
    changed_capabilities = build_capability_evidence(
        platform=snapshot.platform,
        provider="native",
        provider_version="2",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=True,
        supports_background=False,
        supports_process_tree=True,
    )
    mutations = [
        {"intent": intent.model_copy(update={"arguments": {"argv": ["python", "-VV"]}})},
        {"environment": {"PATH": "/bin"}},
        {"secrets": (secrets[0].model_copy(update={"version": "8"}),)},
        {"policy": policy.model_copy(update={"version": "trusted-execution/v2"})},
        {
            "capabilities": changed_capabilities,
            "sandbox": sandbox.model_copy(
                update={"capability_digest": changed_capabilities.evidence_digest}
            ),
        },
    ]
    for mutation in mutations:
        values = {
            "intent": intent,
            "workspace": snapshot,
            "environment": {"PATH": "/usr/bin"},
            "secrets": secrets,
            "sandbox": sandbox,
            "policy": policy,
            "capabilities": capabilities,
            **mutation,
        }
        with pytest.raises(KernelError) as error:
            verify_execution_plan(plan, **values)
        assert error.value.code == "execution_plan_stale"

    target.write_text("after", encoding="utf-8")
    changed = capture_workspace_snapshot(
        tmp_path,
        resources=(WorkspaceResourceRequest(path="main.py", access="write"),),
    )
    with pytest.raises(KernelError) as error:
        verify_execution_plan(
            plan,
            intent=intent,
            workspace=changed,
            environment={"PATH": "/usr/bin"},
            secrets=secrets,
            sandbox=sandbox,
            policy=policy,
            capabilities=capabilities,
        )
    assert error.value.code == "execution_plan_stale"


def test_plan_rejects_tampered_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("before", encoding="utf-8")
    snapshot, capabilities, sandbox, intent, policy, secrets = fixtures(tmp_path)
    plan = build_execution_plan(
        intent,
        snapshot,
        environment={},
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
    )
    data = json.loads(plan.model_dump_json())
    data["fingerprint"] = "0" * 64
    with pytest.raises(ValueError):
        ExecutionPlan.model_validate(data)


def test_windows_environment_names_are_case_insensitive() -> None:
    with pytest.raises(KernelError) as error:
        bind_environment({"Path": "first", "PATH": "second"}, platform="windows")
    assert error.value.code == "execution_environment_denied"


def test_plan_rejects_environment_and_secret_target_collision(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("before", encoding="utf-8")
    snapshot, capabilities, sandbox, intent, policy, secrets = fixtures(tmp_path)
    with pytest.raises(KernelError) as error:
        build_execution_plan(
            intent,
            snapshot,
            environment={"TOKEN": "plain"},
            secrets=secrets,
            sandbox=sandbox,
            policy=policy,
            capabilities=capabilities,
        )
    assert error.value.code == "execution_plan_invalid"
