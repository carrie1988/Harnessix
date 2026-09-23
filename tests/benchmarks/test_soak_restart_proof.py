"""完整产品重启Soak证据合同的离线负向与重读回归。"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_contracts import (
    ProductActionStartupRecoveryReport,
    product_action_startup_recovery_report_digest,
)
from harnessix.trusted_actions.recovery_contracts import (
    ActionRecoveryScanReport,
    action_recovery_scan_report_digest,
)
from scripts.soak_environment import SoakEnvironment
from scripts.soak_evidence import publish_run, read_published_run
from scripts.soak_manifest import SoakManifest, SoakManifestV5
from scripts.soak_restart_proof import (
    PRODUCT_DB_FILES,
    RESTART_PROOF_FILENAME,
    SoakRestartCycle,
    SoakRestartProof,
)
from scripts.soak_rss import RssObservation
from scripts.soak_run_common import publish_measured_run
from scripts.soak_sample_file import sample_sha256
from scripts.soak_samples import SoakSample, validate_sample_series
from tests.benchmarks.test_soak_manifest import RUN_ID, _manifest_data


def _scan(generation: int) -> dict[str, object]:
    data: dict[str, object] = {
        "spec_version": "harnessix.action-recovery-scan/v1",
        "owner_generation": generation,
        "scanned_routes": 0,
        "repaired_execution_plans": 0,
        "invalid_execution_plans": 0,
        "active_operations": 0,
        "expired_operations": 0,
        "process_orphan_leases": 0,
        "session_orphan_references": 0,
        "routes_without_session_reference": 0,
        "artifact_orphans": 0,
        "created_at": datetime.now(UTC),
    }
    provisional = ActionRecoveryScanReport.model_construct(**data, report_sha256="0" * 64)
    return {**data, "report_sha256": action_recovery_scan_report_digest(provisional)}


def _report() -> dict[str, object]:
    data: dict[str, object] = {
        "spec_version": "harnessix.product-action-startup-recovery/v1",
        "candidate_config_sha256": "a" * 64,
        "recovery_config_sha256": "a" * 64,
        "scanned_routes": 0,
        "interrupted_routes": 0,
        "reconciled_routes": 0,
        "succeeded_routes": 0,
        "failed_routes": 0,
        "manual_intervention_routes": 0,
        "unresolved_routes": 0,
        "pending_approval_routes": 0,
        "ready_routes": 0,
        "created_at": datetime.now(UTC),
    }
    provisional = ProductActionStartupRecoveryReport.model_construct(**data, report_sha256="0" * 64)
    return {**data, "report_sha256": product_action_startup_recovery_report_digest(provisional)}


def _input() -> tuple[SoakManifestV5, SoakRestartProof, tuple[SoakSample, ...]]:
    digest = "b" * 64
    cycles = tuple(
        SoakRestartCycle.model_validate(
            {
                "ordinal": ordinal,
                "phase": "warmup" if ordinal == 1 else "crash" if ordinal == 2 else "measure",
                "startup_sample_index": None if ordinal == 2 else ordinal - (ordinal > 2),
                "thread_count": 500,
                "thread_set_sha256": digest,
                "owner_generation": ordinal,
                "recovery_scan": _scan(ordinal),
                "recovery_report": _report(),
                "close_state": "hard_exit" if ordinal == 2 else "closed",
            }
        )
        for ordinal in range(1, 6)
    )
    watermarks = {name: 0 for name in PRODUCT_DB_FILES}
    after = {name: 1024 for name in PRODUCT_DB_FILES}
    proof = SoakRestartProof(
        spec_version="harnessix.soak-restart-proof/v1",
        run_id=RUN_ID,
        thread_count=500,
        thread_set_sha256=digest,
        cycles=cycles,
        hard_exit_ack=True,
        hard_exit_eof=True,
        runner_rss_bytes=4096,
        server_rss_bytes=2048,
        db_before_bytes_by_name=watermarks,
        db_after_bytes_by_name=after,
        wal_before_bytes_by_name=watermarks,
        wal_after_bytes_by_name=watermarks,
    )
    samples = tuple(
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="restart",
            sample_index=index,
            phase="warmup" if index == 1 else "measure",
            metric="product_startup",
            value=100 * index,
            unit="ns",
            clock="monotonic_ns",
        )
        for index in range(1, 5)
    ) + (
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="restart",
            sample_index=5,
            phase="measure",
            metric="rss_peak",
            value=4096,
            unit="bytes",
            rss_source="getrusage",
            rss_raw_unit="KiB",
            rss_normalization="kib_times_1024",
            rss_raw_value=4,
            rss_bytes=4096,
        ),
    )
    counts = {"product_startup": 3, "rss_peak": 1}
    statistics = validate_sample_series(
        samples, run_id=RUN_ID, scenario_id="restart", expected_measured=counts
    )
    proof_digest = sha256((proof.model_dump_json() + "\n").encode()).hexdigest()
    data = _manifest_data(sample_sha256(samples), statistics)
    data.update(
        spec_version="harnessix.soak-manifest/v5",
        scenario_version="harnessix.soak-scenario/v5",
        scenario_id="restart",
        measurement_boundary="product_startup",
        provider={
            "mode": "product_no_turn_v1",
            "script_version": "harnessix.product-no-turn/v1",
            "request_count": 0,
        },
        status="baseline",
        load={
            "turn_count": 0,
            "thread_count": 500,
            "artifact_count": 0,
            "pending_limit": None,
            "warmup_count": 1,
            "fault_matrix_version": "product-restart-v1",
        },
        sample_counts=counts,
        fault_counts={
            "cancelled": 0,
            "timed_out": 0,
            "eof": 1,
            "unknown_effect": 0,
            "duplicate_effect": 0,
            "orphan": 0,
        },
        file_watermarks={
            "db_before_bytes": 0,
            "db_after_bytes": len(PRODUCT_DB_FILES) * 1024,
            "wal_before_bytes": 0,
            "wal_after_bytes": 0,
            "artifact_before_bytes": 0,
            "artifact_after_bytes": 0,
        },
        restart_proof_sha256=proof_digest,
        evidence_sha256={
            "samples.jsonl": sample_sha256(samples),
            RESTART_PROOF_FILENAME: proof_digest,
        },
    )
    return SoakManifestV5.model_validate(data), proof, samples


def test_v5_restart_run_round_trip_and_proof_tamper(tmp_path) -> None:
    manifest, proof, samples = _input()
    run_directory, digest = publish_run(tmp_path / "runs", manifest, samples, restart_proof=proof)
    assert read_published_run(run_directory) == (manifest, digest)

    target = run_directory / RESTART_PROOF_FILENAME
    target.write_bytes(
        target.read_bytes().replace(b'"hard_exit_ack":true', b'"hard_exit_ack":false')
    )
    with pytest.raises(KernelError) as error:
        read_published_run(run_directory)
    assert error.value.code == "soak_run_invalid"


def test_v5_common_publisher_requires_no_turn_provider(tmp_path) -> None:
    manifest, proof, samples = _input()
    environment = SoakEnvironment(
        platform="linux",
        python_version="3.12.10",
        cpu_count=4,
        physical_memory_bytes=8 * 1024**3,
        hardware_class="c4-m8",
    )
    rss = RssObservation(
        source="getrusage",
        raw_value=4,
        raw_unit="KiB",
        normalization="kib_times_1024",
        rss_bytes=4096,
        unit_verified=True,
    )
    run_directory, published = publish_measured_run(
        tmp_path / "runs",
        run_id=manifest.run_id,
        code_revision=manifest.code_revision,
        scenario_id="restart",
        seed=manifest.seed,
        environment=environment,
        started_at=manifest.started_at,
        load=manifest.load,
        samples=samples,
        provider=None,
        restart_proof=proof,
        rss=rss,
        file_watermarks=manifest.file_watermarks,
        baseline=True,
        fault_counts=manifest.fault_counts,
    )
    assert isinstance(published, SoakManifestV5)
    assert published.provider.request_count == 0
    assert read_published_run(run_directory)[0] == published


def test_v5_rejects_short_baseline_and_unverified_legacy_version() -> None:
    manifest, proof, _ = _input()
    short_load = {**manifest.load.model_dump(), "thread_count": 499}
    with pytest.raises(ValidationError, match="正式重启基线"):
        SoakManifestV5.model_validate({**manifest.model_dump(mode="json"), "load": short_load})
    legacy_data = manifest.model_dump(mode="json")
    legacy_data.pop("restart_proof_sha256")
    legacy_data["spec_version"] = "harnessix.soak-manifest/v1"
    legacy_data["scenario_version"] = "harnessix.soak-scenario/v1"
    with pytest.raises(ValidationError, match="产品重启场景必须携带v5证明"):
        SoakManifest.model_validate(legacy_data)
    with pytest.raises(ValidationError, match="Thread集合"):
        SoakRestartProof.model_validate(
            {
                **proof.model_dump(mode="python"),
                "cycles": [
                    {**cycle.model_dump(mode="python"), "thread_set_sha256": "c" * 64}
                    if cycle.ordinal == 3
                    else cycle.model_dump(mode="python")
                    for cycle in proof.cycles
                ],
            }
        )


@pytest.mark.parametrize("field", ["hard_exit_ack", "hard_exit_eof"])
def test_v5_rejects_missing_hard_exit_handshake(field: str) -> None:
    _, proof, _ = _input()
    with pytest.raises(ValidationError, match="ACK或stdio EOF"):
        SoakRestartProof.model_validate({**proof.model_dump(mode="python"), field: False})


def test_v5_rejects_missing_db_file_and_nonincreasing_fence() -> None:
    _, proof, _ = _input()
    files = dict(proof.db_after_bytes_by_name)
    files.pop("sessions.db")
    with pytest.raises(ValidationError, match="SQLite水位"):
        SoakRestartProof.model_validate(
            {**proof.model_dump(mode="python"), "db_after_bytes_by_name": files}
        )
    cycles = list(proof.cycles)
    previous = cycles[1]
    current = cycles[2]
    forged_scan = _scan(previous.owner_generation)
    cycles[2] = SoakRestartCycle.model_validate(
        {
            **current.model_dump(mode="python"),
            "owner_generation": previous.owner_generation,
            "recovery_scan": forged_scan,
        }
    )
    with pytest.raises(ValidationError, match="Owner代际"):
        SoakRestartProof.model_validate({**proof.model_dump(mode="python"), "cycles": cycles})


def test_v5_rejects_provider_request_and_wrong_watermark(tmp_path) -> None:
    manifest, proof, samples = _input()
    invalid_provider = {**manifest.provider.model_dump(), "request_count": 1}
    with pytest.raises(ValidationError, match="不得记录模型请求"):
        SoakManifestV5.model_validate(
            {**manifest.model_dump(mode="python"), "provider": invalid_provider}
        )
    invalid_watermarks = {
        **manifest.file_watermarks.model_dump(),
        "db_after_bytes": manifest.file_watermarks.db_after_bytes + 1,
    }
    forged = SoakManifestV5.model_validate(
        {**manifest.model_dump(mode="python"), "file_watermarks": invalid_watermarks}
    )
    with pytest.raises(KernelError) as error:
        publish_run(tmp_path / "runs", forged, samples, restart_proof=proof)
    assert error.value.code == "soak_restart_proof_invalid"


def test_v5_rejects_action_route_in_no_turn_scenario() -> None:
    _, proof, _ = _input()
    first = proof.cycles[0]
    report = first.recovery_report.model_dump(mode="python")
    report["scanned_routes"] = 1
    provisional = ProductActionStartupRecoveryReport.model_construct(**report)
    report["report_sha256"] = product_action_startup_recovery_report_digest(provisional)
    with pytest.raises(ValidationError, match="不得存在Action Route"):
        SoakRestartCycle.model_validate(
            {**first.model_dump(mode="python"), "recovery_report": report}
        )
