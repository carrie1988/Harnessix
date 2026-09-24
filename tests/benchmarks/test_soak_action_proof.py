"""Action恢复Soak Proof合同与交叉核验回归。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scripts.soak_action_proof import (
    SoakActionCycle,
    SoakActionFault,
    SoakActionProof,
    verify_action_proof,
)
from scripts.soak_rss import RssObservation
from scripts.soak_run_common import latency_sample, rss_sample

RUN_ID = "a" * 32


def _fault(kind: str, *, terminal: str = "succeeded") -> SoakActionFault:
    if kind in {"unknown_outcome", "host_crash"}:
        return SoakActionFault(
            kind=kind, execute_calls=1, reconcile_calls=1, terminal_state=terminal
        )
    return SoakActionFault(kind=kind, execute_calls=0, reconcile_calls=0, terminal_state="none")


def _cycle(ordinal: int, phase: str, sample_index: int, generation: int) -> SoakActionCycle:
    return SoakActionCycle(
        ordinal=ordinal,
        phase=phase,
        faults=(
            _fault("unknown_outcome"),
            _fault("host_crash"),
            _fault("plan_orphan"),
            _fault("artifact_orphan"),
        ),
        owner_generation=generation,
        scanned_routes=ordinal * 2,
        repaired_execution_plans=ordinal,
        artifact_orphans=ordinal,
        scan_report_sha256=f"{ordinal:064x}",
        scan_sample_index=sample_index,
    )


def _proof(cycles: tuple[SoakActionCycle, ...], **overrides: object) -> SoakActionProof:
    data: dict[str, object] = {
        "spec_version": "harnessix.soak-action-proof/v1",
        "run_id": RUN_ID,
        "cycles": cycles,
        "unknown_resolved": len(cycles) * 2,
        "duplicate_effects": 0,
        "crash_exits": len(cycles),
    }
    data.update(overrides)
    return SoakActionProof.model_validate(data)


def _samples(indices: tuple[int, ...], phases: tuple[str, ...]) -> tuple:
    samples = [
        latency_sample(RUN_ID, "action_recovery", index, phase, "recovery_scan", 1000)
        for index, phase in zip(indices, phases, strict=True)
    ]
    samples.append(
        rss_sample(
            RUN_ID,
            "action_recovery",
            len(samples) + 1,
            RssObservation(
                source="getrusage",
                raw_unit="bytes",
                normalization="identity",
                raw_value=1024,
                rss_bytes=1024,
                unit_verified=True,
            ),
        )
    )
    return tuple(samples)


def test_valid_action_proof() -> None:
    proof = _proof((_cycle(1, "warmup", 1, 1), _cycle(2, "measure", 2, 2)))
    assert proof.unknown_resolved == 4
    assert proof.crash_exits == 2


def test_fault_matrix_order_is_fixed() -> None:
    cycle = _cycle(1, "measure", 1, 1).model_dump(mode="json")
    cycle["faults"] = [cycle["faults"][1], *cycle["faults"][:1], *cycle["faults"][2:]]
    with pytest.raises(ValidationError):
        SoakActionCycle.model_validate(cycle)


def test_unknown_fault_requires_single_execute_and_reconcile() -> None:
    with pytest.raises(ValidationError):
        SoakActionFault(
            kind="unknown_outcome", execute_calls=2, reconcile_calls=1, terminal_state="succeeded"
        )
    with pytest.raises(ValidationError):
        SoakActionFault(
            kind="plan_orphan", execute_calls=1, reconcile_calls=0, terminal_state="none"
        )


def test_cycle_counts_must_match_ordinal() -> None:
    cycle = _cycle(2, "measure", 2, 2).model_dump(mode="json")
    cycle["scanned_routes"] = 3
    with pytest.raises(ValidationError):
        SoakActionCycle.model_validate(cycle)


def test_proof_rejects_ordinal_gap_and_stale_generation() -> None:
    with pytest.raises(ValidationError):
        _proof((_cycle(1, "measure", 1, 1), _cycle(3, "measure", 2, 2)))
    with pytest.raises(ValidationError):
        _proof((_cycle(1, "measure", 1, 2), _cycle(2, "measure", 2, 2)))
    with pytest.raises(ValidationError):
        _proof((_cycle(1, "measure", 1, 1),), unknown_resolved=3)


def test_verify_action_proof_cross_checks_samples() -> None:
    proof = _proof((_cycle(1, "warmup", 1, 1), _cycle(2, "measure", 2, 2)))
    samples = _samples((1, 2), ("warmup", "measure"))
    verify_action_proof(
        proof,
        run_id=RUN_ID,
        cycle_count=1,
        warmup_count=1,
        unknown_effect=4,
        samples=samples,
    )
    with pytest.raises(ValueError, match="负载或故障计数不一致"):
        verify_action_proof(
            proof,
            run_id=RUN_ID,
            cycle_count=2,
            warmup_count=1,
            unknown_effect=4,
            samples=samples,
        )
    with pytest.raises(ValueError, match="样本不匹配"):
        verify_action_proof(
            proof,
            run_id=RUN_ID,
            cycle_count=1,
            warmup_count=1,
            unknown_effect=4,
            samples=_samples((1, 3), ("warmup", "measure")),
        )
