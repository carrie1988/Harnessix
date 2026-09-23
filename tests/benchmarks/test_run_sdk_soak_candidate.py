from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_sdk_soak_candidate
from scripts.soak_attempt import SoakAttemptStartV2
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakProfileReference
from scripts.soak_threshold import publish_profile, read_profile


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_frozen_sdk_profile_matches_original_baseline(platform: str, tmp_path: Path) -> None:
    profile_dir = run_sdk_soak_candidate._only_profile(platform)  # noqa: SLF001
    profile, expected_sha = read_profile(profile_dir)
    baseline_dir = (
        run_sdk_soak_candidate._ARCHIVE  # noqa: SLF001
        / "raw"
        / platform
        / profile.baseline_run_id
    )
    baseline, baseline_sha = read_published_run(baseline_dir)
    assert profile.platform == baseline.platform == platform
    assert profile.baseline_manifest_sha256 == baseline_sha
    assert profile.metric_limits["sdk_roundtrip"].margin_basis_points == 10000
    assert profile.metric_limits["rss_peak"].margin_basis_points == 5000
    assert all(item.margin_basis_points == 5000 for item in profile.growth_limits.values())
    _, frozen_sha = publish_profile(tmp_path / platform, profile, baseline_dir)
    assert frozen_sha == expected_sha


def test_profile_discovery_rejects_ambiguous_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_sdk_soak_candidate, "_ARCHIVE", tmp_path)
    platform = tmp_path / "profiles" / "linux"
    (platform / ("a" * 32)).mkdir(parents=True)
    (platform / ("b" * 32)).mkdir()
    with pytest.raises(KernelError) as rejected:
        run_sdk_soak_candidate._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"


async def test_candidate_rejects_baseline_digest_mismatch_before_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = run_sdk_soak_candidate._only_profile("linux")  # noqa: SLF001
    profile, profile_sha = read_profile(profile_dir)
    monkeypatch.setattr(
        run_sdk_soak_candidate,
        "read_environment",
        lambda: SimpleNamespace(platform="linux"),
    )
    monkeypatch.setattr(
        run_sdk_soak_candidate,
        "read_profile",
        lambda _: (
            profile.model_copy(update={"baseline_manifest_sha256": "0" * 64}),
            profile_sha,
        ),
    )

    async def must_not_run(*_args: object, **_kwargs: object):
        raise AssertionError("基线无效时不得执行真实负载")

    monkeypatch.setattr(run_sdk_soak_candidate, "run_sdk_capacity", must_not_run)
    with pytest.raises(KernelError) as rejected:
        await run_sdk_soak_candidate.run_candidate(tmp_path / "evidence", tmp_path / "reports")
    assert rejected.value.code == "soak_profile_baseline_invalid"


async def test_candidate_prebinds_profile_and_uses_fixed_formal_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = "linux"
    profile_dir = run_sdk_soak_candidate._only_profile(platform)  # noqa: SLF001
    profile, profile_sha = read_profile(profile_dir)
    revision = "a" * 40
    candidate_id = "b" * 32
    evidence_root = tmp_path / "evidence"
    report_root = tmp_path / "reports"
    recorded: dict[str, object] = {}
    expected_reference = SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha)
    candidate = SimpleNamespace(
        run_id=candidate_id,
        code_revision=revision,
        status="unverified",
        threshold_profile_ref=None,
    )

    async def run(root: Path, **kwargs: object):
        recorded.update(kwargs)
        assert root == evidence_root
        candidate.threshold_profile_ref = kwargs["threshold_profile_ref"]
        return root / candidate_id, candidate

    actual_read_run = run_sdk_soak_candidate.read_published_run

    def read_run(directory: Path):
        if directory == evidence_root / candidate_id:
            return candidate, "c" * 64
        return actual_read_run(directory)

    actual_read_attempt = run_sdk_soak_candidate.read_attempt

    def read_attempt(directory: Path):
        if directory == evidence_root / "attempts" / candidate_id:
            started = SoakAttemptStartV2(
                spec_version="harnessix.soak-attempt-start/v2",
                run_id=candidate_id,
                code_revision=revision,
                scenario_id="sdk_capacity",
                started_at=datetime.now(UTC),
                threshold_profile_ref=candidate.threshold_profile_ref,
            )
            return started, SimpleNamespace(outcome="committed", manifest_sha256="c" * 64)
        return actual_read_attempt(directory)

    report = SimpleNamespace(status="PASS", reason="within_limits")

    monkeypatch.setattr(
        run_sdk_soak_candidate, "read_environment", lambda: SimpleNamespace(platform=platform)
    )
    monkeypatch.setattr(run_sdk_soak_candidate, "_revision", lambda: revision)
    monkeypatch.setattr(run_sdk_soak_candidate, "run_sdk_capacity", run)
    monkeypatch.setattr(run_sdk_soak_candidate, "read_published_run", read_run)
    monkeypatch.setattr(run_sdk_soak_candidate, "read_attempt", read_attempt)
    monkeypatch.setattr(
        run_sdk_soak_candidate,
        "verify_and_publish",
        lambda *args: (report_root / "report", report),
    )
    monkeypatch.setattr(run_sdk_soak_candidate, "read_report", lambda path: report)

    result = await run_sdk_soak_candidate.run_candidate(evidence_root, report_root)

    assert recorded == {
        "code_revision": revision,
        "measured_rounds": 3,
        "warmup_count": 1,
        "roundtrip_count": 20,
        "pending_limit": 64,
        "timeout_seconds": 30,
        "threshold_profile_ref": candidate.threshold_profile_ref,
    }
    assert candidate.threshold_profile_ref == expected_reference
    assert result["status"] == "PASS"
    assert result["baseline_run_id"] == profile.baseline_run_id


@pytest.mark.parametrize("status,exit_code", [("PASS", 0), ("FAIL", 2), ("unverified", 2)])
def test_candidate_cli_reports_nonpass_as_failure_without_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
) -> None:
    async def result(_evidence: Path, _reports: Path):
        return {
            "status": status,
            "reason": "within_limits" if status == "PASS" else "limit_exceeded",
        }

    monkeypatch.setattr(run_sdk_soak_candidate, "run_candidate", result)
    assert (
        run_sdk_soak_candidate.main(
            ["--evidence-root", str(tmp_path / "secret"), "--report-root", str(tmp_path)]
        )
        == exit_code
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == status
    assert captured.err == "" and str(tmp_path) not in captured.out


def test_candidate_cli_never_logs_private_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(_evidence: Path, _reports: Path):
        raise KernelError("soak_run_invalid", f"private={tmp_path}/secret")

    monkeypatch.setattr(run_sdk_soak_candidate, "run_candidate", fail)
    assert (
        run_sdk_soak_candidate.main(
            ["--evidence-root", str(tmp_path), "--report-root", str(tmp_path)]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "SDK复验失败：soak_run_invalid\n"
    assert str(tmp_path) not in captured.err
