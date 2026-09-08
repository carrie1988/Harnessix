from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPolicyBinding,
    SandboxBindingV2,
)
from harnessix.execution.planner import build_capability_evidence_v2, build_execution_plan_v2
from harnessix.processes.owner_receipt import sign_owner_receipt, verify_owner_receipt
from harnessix.processes.supervision_contracts import (
    ProcessLease,
    ProcessSpec,
    empty_process_output,
)
from harnessix.processes.supervision_planner import (
    build_process_capability,
    build_process_spec,
    prepare_process_lease,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _plan(root: Path, spec: ProcessSpec):
    snapshot = capture_workspace_snapshot(
        root, resources=(WorkspaceResourceRequest(path=".", access="write"),)
    )
    process_capability = build_process_capability(
        platform=snapshot.platform,
        supports_pty=True,
        implementation_digest="a" * 64,
    )
    execution_capability = build_capability_evidence_v2(
        platform=snapshot.platform,
        provider="process_supervisor",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=True,
        supports_background=True,
        supports_process_tree=True,
        provider_evidence_digest=process_capability.digest,
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="full",
        capability_digest=execution_capability.evidence_digest,
        profile_digest="b" * 64,
    )
    intent = ExecutionIntent(
        source="builtin",
        source_id="harnessix",
        tool="process.supervised",
        tool_version="v1",
        tool_fingerprint="c" * 64,
        arguments=spec.model_dump(mode="json", warnings="error"),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        idempotency_key="turn/call",
    )
    plan = build_execution_plan_v2(
        intent,
        snapshot,
        environment={},
        secrets=(),
        sandbox=sandbox,
        policy=ExecutionPolicyBinding(
            version="process/v1",
            decision=PolicyDecisionKind.ALLOW,
            policy_id="process.test",
            reason_code="test",
        ),
        capabilities=execution_capability,
    )
    return plan, process_capability


def test_process_spec_requires_one_exact_invocation_and_self_digest() -> None:
    spec = build_process_spec(
        invocation="argv",
        argv=("python", "-V"),
        input_bytes=0,
        process_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    assert ProcessSpec.model_validate_json(spec.model_dump_json()) == spec
    assert "python" not in repr(spec)
    with pytest.raises(KernelError) as invalid:
        build_process_spec(invocation="argv", argv=(), input_bytes=0)
    assert invalid.value.code == "process_spec_invalid"
    with pytest.raises(KernelError):
        build_process_spec(
            invocation="posix_sh",
            argv=("echo",),
            shell_source="echo ok",
            input_bytes=0,
        )


def test_process_lease_binds_plan_spec_capability_and_deadline(tmp_path: Path) -> None:
    spec = build_process_spec(
        invocation="argv",
        argv=("python", "-V"),
        lifecycle="background",
        timeout_seconds=30,
        input_bytes=0,
    )
    plan, capability = _plan(tmp_path, spec)
    lease = prepare_process_lease(
        plan,
        spec,
        capability,
        now=NOW,
        owner_token="d" * 64,
    )
    assert lease.state == "prepared"
    assert lease.deadline == NOW + timedelta(seconds=30)
    assert lease.process_spec_digest == spec.digest
    assert "d" * 64 not in repr(lease)
    changed = build_process_spec(
        invocation="argv",
        argv=("python", "-VV"),
        lifecycle="background",
        timeout_seconds=30,
        input_bytes=0,
        process_id=spec.process_id,
    )
    with pytest.raises(KernelError) as mismatch:
        prepare_process_lease(plan, changed, capability, now=NOW)
    assert mismatch.value.code == "process_capability_mismatch"


def test_process_lease_rejects_partial_running_and_terminal_facts(tmp_path: Path) -> None:
    spec = build_process_spec(invocation="argv", argv=("python", "-V"), input_bytes=0)
    plan, capability = _plan(tmp_path, spec)
    lease = prepare_process_lease(plan, spec, capability, now=NOW, owner_token="d" * 64)
    with pytest.raises(ValidationError):
        ProcessLease.model_validate(
            {
                **lease.model_dump(mode="json"),
                "state": "running",
                "sequence": 1,
                "pid": 123,
            }
        )
    with pytest.raises(ValidationError):
        ProcessLease.model_validate(
            {
                **lease.model_dump(mode="json"),
                "state": "unknown",
                "sequence": 1,
                "finished_at": NOW.isoformat(),
                "stop_reason": "exited",
            }
        )


def test_process_owner_receipt_mac_binds_identity_and_payload() -> None:
    process_id = UUID("00000000-0000-4000-8000-000000000001")
    receipt = sign_owner_receipt(
        process_id=process_id,
        owner_identity="e" * 64,
        state="running",
        sequence=1,
        owner_token="d" * 64,
        pid=123,
        started_at=NOW,
        stdout=empty_process_output(),
        stderr=empty_process_output(),
    )
    assert (
        verify_owner_receipt(
            receipt,
            owner_token="d" * 64,
            process_id=process_id,
            owner_identity="e" * 64,
        )
        == receipt
    )
    forged = receipt.model_copy(update={"pid": 124})
    with pytest.raises(KernelError) as invalid:
        verify_owner_receipt(forged, owner_token="d" * 64, process_id=process_id)
    assert invalid.value.code == "process_owner_receipt_invalid"
