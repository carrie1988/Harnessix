"""完整产品重启冻结Profile与候选预绑定回归。"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from scripts import run_restart_soak_candidate
from scripts.soak_attempt import SoakAttemptStartV2
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakProfileReference
from scripts.soak_threshold import SoakThresholdProfile, publish_profile, read_profile


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_frozen_restart_evidence_is_not_line_ending_normalized(platform: str) -> None:
    """规范JSON字节必须跨Windows Checkout保持不变。"""

    root = Path(__file__).resolve().parents[2]
    profile_dir = run_restart_soak_candidate._only_profile(platform)  # noqa: SLF001
    profile, _ = read_profile(profile_dir)
    baseline = (
        run_restart_soak_candidate._ARCHIVE  # noqa: SLF001
        / "raw"
        / platform
        / profile.baseline_run_id
        / "manifest.json"
    )
    for path in (profile_dir / "profile.json", baseline):
        relative = path.relative_to(root).as_posix()
        output = subprocess.check_output(
            ("git", "check-attr", "text", "--", relative), cwd=root, text=True
        )
        assert output.strip() == f"{relative}: text: unset"


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_frozen_restart_profile_matches_real_baseline(platform: str, tmp_path: Path) -> None:
    profile_dir = run_restart_soak_candidate._only_profile(platform)  # noqa: SLF001
    profile, expected_sha = read_profile(profile_dir)
    baseline_dir = (
        run_restart_soak_candidate._ARCHIVE  # noqa: SLF001
        / "raw"
        / platform
        / profile.baseline_run_id
    )
    baseline, baseline_sha = read_published_run(baseline_dir)
    assert profile.platform == baseline.platform == platform
    assert profile.baseline_manifest_sha256 == baseline_sha
    assert profile.provider_script_version == "harnessix.product-no-turn/v1"
    assert profile.metric_limits["product_startup"].margin_basis_points == 10000
    assert profile.metric_limits["rss_peak"].margin_basis_points == 5000
    assert all(item.margin_basis_points == 5000 for item in profile.growth_limits.values())
    _, frozen_sha = publish_profile(tmp_path / platform, profile, baseline_dir)
    assert frozen_sha == expected_sha


@pytest.mark.parametrize(
    ("scenario", "provider"),
    [
        ("restart", "harnessix.soak-provider/v1"),
        ("sdk_capacity", "harnessix.product-no-turn/v1"),
    ],
)
def test_provider_script_version_is_scenario_specific(scenario: str, provider: str) -> None:
    profile_dir = run_restart_soak_candidate._only_profile("linux")  # noqa: SLF001
    profile, _ = read_profile(profile_dir)
    data = profile.model_dump(mode="json")
    data["scenario_id"] = scenario
    data["provider_script_version"] = provider
    with pytest.raises(ValidationError):
        SoakThresholdProfile.model_validate(data)


def test_profile_discovery_rejects_ambiguous_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_restart_soak_candidate, "_ARCHIVE", tmp_path)
    platform = tmp_path / "profiles" / "linux"
    (platform / ("a" * 32)).mkdir(parents=True)
    (platform / ("b" * 32)).mkdir()
    with pytest.raises(KernelError) as rejected:
        run_restart_soak_candidate._only_profile("linux")  # noqa: SLF001
    assert rejected.value.code == "soak_profile_invalid"


async def test_candidate_rejects_baseline_digest_mismatch_before_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = run_restart_soak_candidate._only_profile("linux")  # noqa: SLF001
    profile, profile_sha = read_profile(profile_dir)
    monkeypatch.setattr(
        run_restart_soak_candidate,
        "read_environment",
        lambda: SimpleNamespace(platform="linux"),
    )
    monkeypatch.setattr(
        run_restart_soak_candidate,
        "read_profile",
        lambda _: (
            profile.model_copy(update={"baseline_manifest_sha256": "0" * 64}),
            profile_sha,
        ),
    )

    async def must_not_run(*_args: object, **_kwargs: object):
        raise AssertionError("基线无效时不得执行真实负载")

    monkeypatch.setattr(run_restart_soak_candidate, "run_product_restart", must_not_run)
    with pytest.raises(KernelError) as rejected:
        await run_restart_soak_candidate.run_candidate(
            tmp_path / "evidence", tmp_path / "reports", baseline_set="legacy-unprotected-v1"
        )
    assert rejected.value.code == "soak_profile_baseline_invalid"


@pytest.mark.parametrize("baseline_set", ["legacy-unprotected-v1", "authenticated-v1"])
async def test_candidate_prebinds_profile_and_uses_fixed_formal_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline_set: str
) -> None:
    platform = "linux"
    archive = run_restart_soak_candidate._archive_for_set(baseline_set)  # noqa: SLF001
    profile_dir = run_restart_soak_candidate._only_profile(platform, archive=archive)  # noqa: SLF001
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

    actual_read_run = run_restart_soak_candidate.read_published_run

    def read_run(directory: Path):
        if directory == evidence_root / candidate_id:
            return candidate, "c" * 64
        return actual_read_run(directory)

    actual_read_attempt = run_restart_soak_candidate.read_attempt

    def read_attempt(directory: Path):
        if directory == evidence_root / "attempts" / candidate_id:
            started = SoakAttemptStartV2(
                spec_version="harnessix.soak-attempt-start/v2",
                run_id=candidate_id,
                code_revision=revision,
                scenario_id="restart",
                started_at=datetime.now(UTC),
                threshold_profile_ref=candidate.threshold_profile_ref,
            )
            return started, SimpleNamespace(outcome="committed", manifest_sha256="c" * 64)
        return actual_read_attempt(directory)

    report = SimpleNamespace(status="PASS", reason="within_limits")
    monkeypatch.setattr(
        run_restart_soak_candidate, "read_environment", lambda: SimpleNamespace(platform=platform)
    )
    monkeypatch.setattr(run_restart_soak_candidate, "_revision", lambda: revision)
    monkeypatch.setattr(run_restart_soak_candidate, "run_product_restart", run)
    monkeypatch.setattr(run_restart_soak_candidate, "read_published_run", read_run)
    monkeypatch.setattr(run_restart_soak_candidate, "read_attempt", read_attempt)
    monkeypatch.setattr(
        run_restart_soak_candidate,
        "verify_and_publish",
        lambda *args: (report_root / "report", report),
    )
    monkeypatch.setattr(run_restart_soak_candidate, "read_report", lambda path: report)

    result = await run_restart_soak_candidate.run_candidate(
        evidence_root, report_root, baseline_set=baseline_set
    )

    assert recorded == {
        "code_revision": revision,
        "thread_count": 500,
        "warmup_count": 1,
        "measured_restarts": 3,
        "timeout_seconds": 120,
        "threshold_profile_ref": expected_reference,
    }
    assert result["status"] == "PASS"
    assert result["baseline_run_id"] == profile.baseline_run_id
    assert result["baseline_set"] == baseline_set


@pytest.mark.parametrize("status,exit_code", [("PASS", 0), ("FAIL", 2), ("unverified", 2)])
def test_candidate_cli_reports_nonpass_as_failure_without_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
) -> None:
    async def result(_evidence: Path, _reports: Path, *, baseline_set: str = "authenticated-v1"):
        return {
            "status": status,
            "reason": "within_limits" if status == "PASS" else "limit_exceeded",
        }

    monkeypatch.setattr(run_restart_soak_candidate, "run_candidate", result)
    assert (
        run_restart_soak_candidate.main(
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
    async def fail(_evidence: Path, _reports: Path, *, baseline_set: str = "authenticated-v1"):
        raise KernelError("soak_run_invalid", f"private={tmp_path}/secret")

    monkeypatch.setattr(run_restart_soak_candidate, "run_candidate", fail)
    assert (
        run_restart_soak_candidate.main(
            ["--evidence-root", str(tmp_path), "--report-root", str(tmp_path)]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "产品重启复验失败：soak_run_invalid\n"
    assert str(tmp_path) not in captured.err


async def test_unknown_baseline_set_is_rejected_before_environment_or_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_read_environment():
        raise AssertionError("无效集合不得开始环境或负载读取")

    monkeypatch.setattr(run_restart_soak_candidate, "read_environment", must_not_read_environment)
    with pytest.raises(KernelError) as rejected:
        await run_restart_soak_candidate.run_candidate(
            tmp_path / "evidence", tmp_path / "reports", baseline_set="unknown"
        )
    assert rejected.value.code == "soak_profile_invalid"


async def test_missing_authenticated_baseline_never_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_restart_soak_candidate, "_AUTHENTICATED_ARCHIVE", tmp_path / "absent")
    monkeypatch.setattr(
        run_restart_soak_candidate, "read_environment", lambda: SimpleNamespace(platform="linux")
    )
    with pytest.raises(KernelError) as rejected:
        await run_restart_soak_candidate.run_candidate(
            tmp_path / "evidence", tmp_path / "reports", baseline_set="authenticated-v1"
        )
    assert rejected.value.code == "soak_profile_invalid"
    assert not (tmp_path / "evidence").exists()


def test_candidate_cli_passes_explicit_authenticated_baseline_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def selected(_evidence: Path, _reports: Path, *, baseline_set: str):
        assert baseline_set == "authenticated-v1"
        return {"baseline_set": baseline_set, "status": "PASS", "reason": "within_limits"}

    monkeypatch.setattr(run_restart_soak_candidate, "run_candidate", selected)
    assert (
        run_restart_soak_candidate.main(
            [
                "--evidence-root",
                str(tmp_path / "private"),
                "--report-root",
                str(tmp_path / "reports"),
                "--baseline-set",
                "authenticated-v1",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["baseline_set"] == "authenticated-v1"
    assert str(tmp_path) not in output.out and output.err == ""


async def test_authenticated_selection_rejects_actual_legacy_run_before_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        run_restart_soak_candidate,
        "_AUTHENTICATED_ARCHIVE",
        run_restart_soak_candidate._ARCHIVE,  # noqa: SLF001
    )
    monkeypatch.setattr(
        run_restart_soak_candidate, "read_environment", lambda: SimpleNamespace(platform="linux")
    )

    async def must_not_run(*_args: object, **_kwargs: object):
        raise AssertionError("历史未认证Run不能成为认证候选基线")

    monkeypatch.setattr(run_restart_soak_candidate, "run_product_restart", must_not_run)
    with pytest.raises(KernelError) as rejected:
        await run_restart_soak_candidate.run_candidate(
            tmp_path / "evidence", tmp_path / "reports", baseline_set="authenticated-v1"
        )
    assert rejected.value.code == "soak_profile_baseline_invalid"
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize(
    "change", ["source", "load", "samples", "faults", "python", "startup_margin", "db_margin"]
)
def test_authenticated_policy_rejects_source_and_original_rule_drift(change: str) -> None:
    """这里只验证策略拒绝，内存复制不作为真实认证运行证据。"""

    profile_dir = run_restart_soak_candidate._only_profile("linux")  # noqa: SLF001
    profile, _ = read_profile(profile_dir)
    baseline, _ = read_published_run(
        run_restart_soak_candidate._ARCHIVE  # noqa: SLF001
        / "raw"
        / "linux"
        / profile.baseline_run_id
    )
    baseline = baseline.model_copy(
        update={"code_revision": run_restart_soak_candidate._AUTHENTICATED_BASELINE_REVISION}  # noqa: SLF001
    )
    if change == "source":
        baseline = baseline.model_copy(update={"code_revision": "0" * 40})
    elif change == "load":
        profile = profile.model_copy(
            update={"load": profile.load.model_copy(update={"thread_count": 499})}
        )
    elif change == "samples":
        profile = profile.model_copy(
            update={"sample_counts": {"product_startup": 4, "rss_peak": 1}}
        )
    elif change == "faults":
        profile = profile.model_copy(
            update={
                "expected_fault_counts": profile.expected_fault_counts.model_copy(update={"eof": 2})
            }
        )
    elif change == "python":
        profile = profile.model_copy(update={"python_max": "3.13.99"})
    elif change == "startup_margin":
        limits = dict(profile.metric_limits)
        limits["product_startup"] = limits["product_startup"].model_copy(
            update={"margin_basis_points": 9999}
        )
        profile = profile.model_copy(update={"metric_limits": limits})
    else:
        limits = dict(profile.growth_limits)
        limits["db"] = limits["db"].model_copy(update={"margin_basis_points": 5001})
        profile = profile.model_copy(update={"growth_limits": limits})
    with pytest.raises(KernelError) as rejected:
        run_restart_soak_candidate._check_authenticated_policy(profile, baseline)  # noqa: SLF001
    assert rejected.value.code == "soak_profile_baseline_invalid"


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_frozen_authenticated_profile_binds_source_load_and_original_policy(
    platform: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    archive = run_restart_soak_candidate._archive_for_set("authenticated-v1")  # noqa: SLF001
    directory = run_restart_soak_candidate._only_profile(platform, archive=archive)  # noqa: SLF001
    profile, digest = read_profile(directory)
    baseline_path = archive / "raw" / platform / profile.baseline_run_id
    baseline, _ = read_published_run(baseline_path)
    run_restart_soak_candidate._check_authenticated_policy(profile, baseline)  # noqa: SLF001
    copied, copied_digest = publish_profile(tmp_path / platform, profile, baseline_path)
    assert read_profile(copied) == (profile, digest) and copied_digest == digest
    legacy, _ = read_profile(run_restart_soak_candidate._only_profile(platform))  # noqa: SLF001
    assert profile.profile_id != legacy.profile_id
    assert baseline.status == "baseline" and baseline.provider.request_count == 0
    assert profile.load.thread_count == 500 and profile.load.warmup_count == 1
    for path in (directory / "profile.json", baseline_path / "manifest.json"):
        relative = path.relative_to(root).as_posix()
        attribute = subprocess.check_output(
            ("git", "check-attr", "text", "--", relative), cwd=root, text=True
        ).strip()
        assert attribute == f"{relative}: text: unset"


async def test_candidate_rejects_invalid_mathematical_limit_before_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = run_restart_soak_candidate._only_profile("linux")  # noqa: SLF001
    original, digest = read_profile(directory)
    limits = dict(original.metric_limits)
    limits["product_startup"] = limits["product_startup"].model_copy(
        update={"p95_upper": limits["product_startup"].p95_upper + 1}
    )
    invalid = original.model_copy(update={"metric_limits": limits})
    monkeypatch.setattr(run_restart_soak_candidate, "read_profile", lambda _: (invalid, digest))
    monkeypatch.setattr(
        run_restart_soak_candidate, "read_environment", lambda: SimpleNamespace(platform="linux")
    )

    async def must_not_run(*_args: object, **_kwargs: object):
        raise AssertionError("无效数学阈值必须在负载前拒绝")

    monkeypatch.setattr(run_restart_soak_candidate, "run_product_restart", must_not_run)
    with pytest.raises(KernelError) as rejected:
        await run_restart_soak_candidate.run_candidate(
            tmp_path / "evidence", tmp_path / "reports", baseline_set="legacy-unprotected-v1"
        )
    assert rejected.value.code == "soak_profile_baseline_invalid"
    assert not (tmp_path / "evidence").exists()
