from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from scripts.soak_manifest import SoakManifest, verify_manifest_samples
from scripts.soak_sample_file import SAMPLE_FILENAME, write_sample_file
from scripts.soak_samples import SoakSample

RUN_ID = uuid4().hex
COUNTS = {"turn_local": 1, "rss_peak": 1}


def _samples(rss_source: str = "getrusage") -> tuple[SoakSample, ...]:
    return (
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="long_session",
            sample_index=1,
            phase="measure",
            metric="turn_local",
            value=100,
            unit="ns",
            clock="monotonic_ns",
        ),
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="long_session",
            sample_index=2,
            phase="measure",
            metric="rss_peak",
            value=4096,
            unit="bytes",
            rss_source=rss_source,
            rss_raw_unit="KiB",
            rss_normalization="kib_times_1024",
            rss_raw_value=4,
            rss_bytes=4096,
        ),
    )


def _manifest_data(digest: str, statistics: dict[str, object]) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "spec_version": "harnessix.soak-manifest/v1",
        "run_id": RUN_ID,
        "code_revision": "a" * 40,
        "scenario_version": "harnessix.soak-scenario/v1",
        "scenario_id": "long_session",
        "measurement_boundary": "core_runtime",
        "seed": 11,
        "provider": {
            "mode": "deterministic_stateless_v1",
            "script_version": "harnessix.soak-provider/v1",
            "request_count": 1,
        },
        "platform": "linux",
        "python_version": "3.12.10",
        "cpu_count": 4,
        "physical_memory_bytes": 8 * 1024**3,
        "hardware_class": "ci-small",
        "started_at": now,
        "ended_at": now,
        "status": "unverified",
        "load": {
            "turn_count": 1,
            "thread_count": 1,
            "artifact_count": 0,
            "warmup_count": 0,
        },
        "sample_counts": COUNTS,
        "quantile_method": "nearest_rank_v1",
        "statistics": statistics,
        "rss": {
            "source": "getrusage",
            "raw_unit": "KiB",
            "normalization": "kib_times_1024",
            "peak_bytes": 4096,
            "unit_verified": True,
        },
        "file_watermarks": {
            "db_before_bytes": 0,
            "db_after_bytes": 4096,
            "wal_before_bytes": 0,
            "wal_after_bytes": 0,
            "artifact_before_bytes": 0,
            "artifact_after_bytes": 0,
        },
        "fault_counts": {
            "cancelled": 0,
            "timed_out": 0,
            "eof": 0,
            "unknown_effect": 0,
            "duplicate_effect": 0,
            "orphan": 0,
        },
        "evidence_sha256": {SAMPLE_FILENAME: digest},
        "threshold_profile_ref": None,
    }


def test_manifest_recomputes_file_statistics_and_rejects_spoofed_summary(tmp_path) -> None:
    digest, statistics = write_sample_file(
        tmp_path,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    data = _manifest_data(digest, statistics)
    manifest = SoakManifest.model_validate(data)

    verify_manifest_samples(manifest, tmp_path)
    forged = {
        **data,
        "statistics": {
            **statistics,
            "turn_local": {"sample_count": 1, "p50": 1, "p95": 1, "p99": 1},
        },
    }
    with pytest.raises(ValueError, match="不一致"):
        verify_manifest_samples(SoakManifest.model_validate(forged), tmp_path)
    (tmp_path / SAMPLE_FILENAME).write_bytes(b"{}\n")
    with pytest.raises(KernelError):
        verify_manifest_samples(manifest, tmp_path)


def test_manifest_rejects_wrong_boundary_rss_platform_and_sensitive_field(tmp_path) -> None:
    digest, statistics = write_sample_file(
        tmp_path,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    data = _manifest_data(digest, statistics)
    for changes in (
        {"measurement_boundary": "sdk_stdio"},
        {"platform": "windows"},
        {"prompt": "不得入证据"},
        {"sample_counts": {"turn_local": 1}},
        {"status": "verified"},
        {"status": "baseline"},
    ):
        with pytest.raises(ValidationError):
            SoakManifest.model_validate({**data, **changes})


def test_manifest_requires_frozen_rss_unit_for_baseline(tmp_path) -> None:
    digest, statistics = write_sample_file(
        tmp_path,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    data = _manifest_data(digest, statistics)
    data["status"] = "baseline"
    data["load"] = {"turn_count": 1000, "thread_count": 1, "artifact_count": 0, "warmup_count": 0}
    data["rss"] = {**data["rss"], "unit_verified": False}

    with pytest.raises(ValidationError, match="RSS单位"):
        SoakManifest.model_validate(data)
    data["rss"] = {**data["rss"], "unit_verified": True}
    with pytest.raises(ValidationError, match="样本或模型请求数不足"):
        SoakManifest.model_validate(data)


def test_linux_proc_status_source_does_not_change_historical_getrusage_reader(tmp_path) -> None:
    historical_root = tmp_path / "historical"
    historical_root.mkdir()
    digest, statistics = write_sample_file(
        historical_root,
        _samples(),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    historical = _manifest_data(digest, statistics)
    historical_manifest = SoakManifest.model_validate(historical)
    assert historical_manifest.rss.source == "getrusage"
    verify_manifest_samples(historical_manifest, historical_root)

    current_root = tmp_path / "current"
    current_root.mkdir()
    current_digest, current_statistics = write_sample_file(
        current_root,
        _samples("proc_status"),
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    current = _manifest_data(current_digest, current_statistics)
    current["rss"] = {**current["rss"], "source": "proc_status"}
    current_manifest = SoakManifest.model_validate(current)
    verify_manifest_samples(current_manifest, current_root)
    with pytest.raises(ValidationError, match="只适用于Linux"):
        SoakManifest.model_validate({**current, "platform": "macos"})
