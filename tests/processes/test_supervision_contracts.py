from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import harnessix.processes.owner_receipt as owner_receipt_module
from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    SandboxBindingV2,
)
from harnessix.execution.planner import build_capability_evidence_v2, build_execution_plan_v2
from harnessix.processes.owner_receipt import (
    read_owner_receipt,
    sign_owner_receipt,
    verify_owner_receipt,
    write_owner_receipt,
)
from harnessix.processes.supervision_contracts import (
    ProcessCapabilityProbe,
    ProcessLease,
    ProcessSpec,
    empty_process_output,
)
from harnessix.processes.supervision_planner import (
    build_process_capability,
    build_process_launch_binding,
    build_process_spec,
    prepare_process_lease,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _plan(root: Path, spec: ProcessSpec) -> tuple[ExecutionPlanV2, ProcessCapabilityProbe]:
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
    assert lease.launch_binding_digest != capability.digest
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


def test_process_launch_binding_covers_plan_materialization_and_environment(tmp_path: Path) -> None:
    spec = build_process_spec(invocation="argv", argv=("python", "-V"))
    plan, capability = _plan(tmp_path, spec)
    binding = build_process_launch_binding(
        plan,
        spec,
        capability,
        kind="host",
        environment={},
    )
    assert binding.plan_fingerprint == plan.fingerprint
    assert binding.process_spec_digest == spec.digest
    assert binding.capability_digest == capability.digest
    with pytest.raises(ValidationError):
        type(binding).model_validate_json(
            binding.model_copy(update={"process_spec_digest": "f" * 64}).model_dump_json()
        )


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


def test_process_owner_receipt_reads_short_regular_file_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    path = tmp_path / "receipt.json"
    write_owner_receipt(path, receipt)
    real_read = os.read
    monkeypatch.setattr(
        os,
        "read",
        lambda descriptor, size: real_read(descriptor, min(size, 7)),
    )
    assert (
        read_owner_receipt(
            path,
            owner_token="d" * 64,
            process_id=process_id,
            owner_identity="e" * 64,
        )
        == receipt
    )


def test_process_owner_receipt_retries_windows_sharing_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    path = tmp_path / "receipt.json"
    write_owner_receipt(path, receipt)
    read_once = owner_receipt_module._read_owner_receipt_once  # noqa: SLF001
    calls = 0

    def sharing_then_read(candidate: Path):
        nonlocal calls
        calls += 1
        if calls < 3:
            error = PermissionError("sharing violation")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        return read_once(candidate)

    monkeypatch.setattr(owner_receipt_module, "_read_owner_receipt_once", sharing_then_read)
    monkeypatch.setattr(owner_receipt_module, "_WINDOWS_RECEIPT_READ_DELAYS", (0.0,) * 7)

    assert (
        read_owner_receipt(
            path,
            owner_token="d" * 64,
            process_id=process_id,
            owner_identity="e" * 64,
        )
        == receipt
    )
    assert calls == 3


def test_process_owner_receipt_bounds_persistent_windows_sharing_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def always_conflicted(_candidate: Path):
        nonlocal calls
        calls += 1
        error = PermissionError("sharing violation")
        error.winerror = 32  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(owner_receipt_module, "_read_owner_receipt_once", always_conflicted)
    monkeypatch.setattr(owner_receipt_module, "_WINDOWS_RECEIPT_READ_DELAYS", (0.0,) * 7)

    with pytest.raises(KernelError) as invalid:
        read_owner_receipt(
            tmp_path / "receipt.json",
            owner_token="d" * 64,
            process_id=UUID("00000000-0000-4000-8000-000000000001"),
        )

    assert invalid.value.code == "process_owner_receipt_invalid"
    assert calls == 7


def test_process_owner_receipt_does_not_retry_invalid_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def invalid_content(_candidate: Path):
        nonlocal calls
        calls += 1
        raise ValueError("invalid JSON")

    monkeypatch.setattr(owner_receipt_module, "_read_owner_receipt_once", invalid_content)
    monkeypatch.setattr(owner_receipt_module, "_WINDOWS_RECEIPT_READ_DELAYS", (0.0,) * 7)

    with pytest.raises(KernelError) as invalid:
        read_owner_receipt(
            tmp_path / "receipt.json",
            owner_token="d" * 64,
            process_id=UUID("00000000-0000-4000-8000-000000000001"),
        )

    assert invalid.value.code == "process_owner_receipt_invalid"
    assert calls == 1
