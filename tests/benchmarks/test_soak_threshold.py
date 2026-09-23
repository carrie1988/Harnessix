from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from scripts.soak_attempt import attempt_scope
from scripts.soak_environment import read_environment
from scripts.soak_manifest import (
    SoakFaultCounts,
    SoakFileWatermarks,
    SoakLoad,
    SoakProfileReference,
)
from scripts.soak_provider import SoakProvider
from scripts.soak_rss import read_peak_rss
from scripts.soak_run_common import latency_sample, publish_measured_run, rss_sample
from scripts.soak_threshold import (
    SoakGrowthLimit,
    SoakMetricLimit,
    SoakThresholdProfile,
    publish_profile,
    read_profile,
    read_report,
    verify_and_publish,
)

REVISION = "a" * 40


def _run(root, *, latency: int, complete_attempt: bool = True, profile_ref=None):
    run_id = uuid4().hex
    rss = read_peak_rss()
    samples = tuple(
        latency_sample(run_id, "many_threads", index, "measure", metric, latency)
        for index, metric in enumerate(
            (
                "app_service_startup",
                "thread_list_page",
                "app_service_startup",
                "thread_list_page",
                "app_service_startup",
                "thread_list_page",
            ),
            start=1,
        )
    ) + (rss_sample(run_id, "many_threads", 7, rss),)
    try:
        with attempt_scope(
            root, run_id=run_id, code_revision=REVISION, scenario_id="many_threads"
        ) as attempt:
            directory, manifest = publish_measured_run(
                root,
                run_id=run_id,
                code_revision=REVISION,
                scenario_id="many_threads",
                seed=17,
                environment=read_environment(),
                started_at=datetime.now(UTC),
                load=SoakLoad(turn_count=0, thread_count=500, artifact_count=0, warmup_count=0),
                samples=samples,
                provider=SoakProvider(),
                rss=rss,
                file_watermarks=SoakFileWatermarks(
                    db_before_bytes=100,
                    db_after_bytes=200,
                    wal_before_bytes=0,
                    wal_after_bytes=0,
                    artifact_before_bytes=0,
                    artifact_after_bytes=0,
                ),
                baseline=profile_ref is None,
                threshold_profile_ref=profile_ref,
            )
            if complete_attempt:
                attempt.commit(directory)
            else:
                # 模拟Run已提交、Attempt尚未提交的中断；不补写终态。
                raise RuntimeError("提交窗口中断")
    except RuntimeError:
        if complete_attempt:
            raise
    return directory, manifest


def _profile(manifest, digest, *, margin: int = 1000):
    limits = {
        metric: SoakMetricLimit(
            unit="bytes" if metric == "rss_peak" else "ns",
            direction="upper",
            margin_basis_points=margin,
            p50_upper=(stats.p50 * (10_000 + margin) + 9_999) // 10_000,
            p95_upper=(stats.p95 * (10_000 + margin) + 9_999) // 10_000,
            p99_upper=(stats.p99 * (10_000 + margin) + 9_999) // 10_000,
        )
        for metric, stats in manifest.statistics.items()
    }
    growth = {
        "db": 100,
        "wal": 0,
        "artifact": 0,
    }
    return SoakThresholdProfile(
        spec_version="harnessix.soak-threshold/v1",
        profile_id=uuid4().hex,
        status="frozen",
        scenario_id=manifest.scenario_id,
        scenario_version=manifest.scenario_version,
        measurement_boundary=manifest.measurement_boundary,
        platform=manifest.platform,
        hardware_class=manifest.hardware_class,
        python_min=manifest.python_version,
        python_max=manifest.python_version,
        baseline_run_id=manifest.run_id,
        baseline_manifest_sha256=digest,
        seed=manifest.seed,
        load=manifest.load,
        provider_script_version=manifest.provider.script_version,
        sample_counts=manifest.sample_counts,
        quantile_method="nearest_rank_v1",
        metric_limits=limits,
        growth_limits={
            kind: SoakGrowthLimit(
                unit="bytes",
                direction="upper",
                margin_basis_points=margin,
                upper_bytes=(value * (10_000 + margin) + 9_999) // 10_000,
            )
            for kind, value in growth.items()
        },
        expected_fault_counts=SoakFaultCounts(
            cancelled=0,
            timed_out=0,
            eof=0,
            unknown_effect=0,
            duplicate_effect=0,
            orphan=0,
        ),
        required_platform_validation=("linux", "macos", "windows"),
        created_at=datetime.now(UTC),
    )


def test_independent_run_passes_only_with_frozen_complete_evidence(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    profile_dir, profile_sha = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    assert read_profile(profile_dir) == (profile, profile_sha)
    candidate_dir, candidate = _run(
        tmp_path / "candidate",
        latency=105,
        profile_ref=SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha),
    )
    assert candidate.status == "unverified"
    assert candidate.threshold_profile_ref == SoakProfileReference(
        profile_id=profile.profile_id, sha256=profile_sha
    )

    report_dir, report = verify_and_publish(
        profile_dir, baseline_dir, candidate_dir, tmp_path / "reports"
    )
    assert report.status == "PASS" and report.reason == "within_limits"
    assert report.violations == ()
    assert report.baseline_manifest_sha256 == digest
    assert report.candidate_manifest_sha256 == read_published_run(candidate_dir)[1]
    assert read_report(report_dir) == report
    raw = report_dir.joinpath("report.json").read_bytes()
    assert str(tmp_path).encode() not in raw
    assert b"session.db" not in raw


def test_exceeded_limit_and_same_run_fail_closed(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    profile_dir, profile_sha = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    candidate_dir, _ = _run(
        tmp_path / "candidate",
        latency=150,
        profile_ref=SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha),
    )
    _, report = verify_and_publish(profile_dir, baseline_dir, candidate_dir, tmp_path / "reports")
    assert report.status == "FAIL" and report.reason == "limit_exceeded"
    assert "app_service_startup.p95" in report.violations
    _, repeated = verify_and_publish(profile_dir, baseline_dir, baseline_dir, tmp_path / "reports")
    assert repeated.status == "unverified" and repeated.reason == "load_mismatch"


def test_candidate_without_bound_profile_cannot_pass(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    profile_dir, _ = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    candidate_dir, _ = _run(tmp_path / "candidate", latency=100)
    _, report = verify_and_publish(profile_dir, baseline_dir, candidate_dir, tmp_path / "reports")
    assert report.status == "unverified" and report.reason == "profile_mismatch"
    wrong_dir, _ = _run(
        tmp_path / "wrong-candidate",
        latency=100,
        profile_ref=SoakProfileReference(profile_id=profile.profile_id, sha256="0" * 64),
    )
    _, wrong = verify_and_publish(profile_dir, baseline_dir, wrong_dir, tmp_path / "reports")
    assert wrong.status == "unverified" and wrong.reason == "profile_mismatch"


def test_tamper_incomplete_attempt_and_invalid_profile_cannot_pass(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    profile_dir, profile_sha = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    candidate_dir, _ = _run(
        tmp_path / "candidate",
        latency=100,
        complete_attempt=False,
        profile_ref=SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha),
    )
    _, report = verify_and_publish(profile_dir, baseline_dir, candidate_dir, tmp_path / "reports")
    assert report.status == "unverified" and report.reason == "evidence_invalid"
    assert report.candidate_manifest_sha256 is None

    profile_dir.joinpath("profile.json").write_bytes(b"{}\n")
    with pytest.raises(KernelError) as error:
        verify_and_publish(profile_dir, baseline_dir, candidate_dir, tmp_path / "reports")
    assert error.value.code == "soak_profile_invalid"


def test_candidate_tamper_is_persisted_as_unverified_and_report_is_sealed(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    profile_dir, profile_sha = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    candidate_dir, _ = _run(
        tmp_path / "candidate",
        latency=100,
        profile_ref=SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha),
    )
    sample_file = candidate_dir / "samples.jsonl"
    sample_file.write_bytes(sample_file.read_bytes().replace(b'"value":100', b'"value":999', 1))
    report_dir, report = verify_and_publish(
        profile_dir, baseline_dir, candidate_dir, tmp_path / "reports"
    )
    assert report.status == "unverified" and report.reason == "evidence_invalid"
    assert read_report(report_dir) == report
    report_dir.joinpath("report.json").write_bytes(b"{}\n")
    with pytest.raises(KernelError) as error:
        read_report(report_dir)
    assert error.value.code == "soak_report_invalid"


def test_profile_and_report_paths_are_non_overwriting(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    directory, _ = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    original = (directory / "profile.json").read_bytes()
    with pytest.raises(KernelError) as error:
        publish_profile(tmp_path / "profiles", profile, baseline_dir)
    assert error.value.code == "soak_evidence_exists"
    assert (directory / "profile.json").read_bytes() == original


def test_profile_threshold_must_equal_expanded_baseline(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    limits = dict(profile.metric_limits)
    limits["thread_list_page"] = limits["thread_list_page"].model_copy(update={"p95_upper": 1})
    wrong = profile.model_copy(update={"metric_limits": limits})
    with pytest.raises(KernelError) as error:
        publish_profile(tmp_path / "profiles", wrong, baseline_dir)
    assert error.value.code == "soak_profile_baseline_invalid"
    assert not (tmp_path / "profiles").exists()


def test_profile_must_declare_all_three_release_platforms(tmp_path) -> None:
    from scripts.soak_evidence import read_published_run

    baseline_dir, baseline = _run(tmp_path / "baseline", latency=100)
    _, digest = read_published_run(baseline_dir)
    profile = _profile(baseline, digest)
    payload = profile.model_dump()
    payload["required_platform_validation"] = (baseline.platform,)
    with pytest.raises(ValidationError, match="平台|环境范围"):
        SoakThresholdProfile.model_validate(payload)
