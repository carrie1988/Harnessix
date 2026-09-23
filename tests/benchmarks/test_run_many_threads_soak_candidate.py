from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from scripts import run_many_threads_soak_candidate as candidate_script
from scripts.soak_manifest import SoakProfileReference
from scripts.soak_threshold import publish_profile, read_profile
from tests.benchmarks.test_soak_threshold import REVISION, _profile, _run


def _summary(*, status: str = "PASS", reason: str = "within_limits") -> dict[str, str]:
    return {
        "scenario_id": "many_threads",
        "platform": "macos",
        "code_revision": "a" * 40,
        "profile_id": "b" * 32,
        "baseline_run_id": "c" * 32,
        "candidate_run_id": "d" * 32,
        "status": status,
        "reason": reason,
    }


@pytest.mark.parametrize(
    ("status", "reason", "accepted"),
    [
        ("PASS", "within_limits", True),
        ("FAIL", "limit_exceeded", True),
        ("unverified", "evidence_invalid", True),
        ("PASS", "limit_exceeded", False),
        ("FAIL", "within_limits", False),
        ("PASS", "private=/secret", False),
    ],
)
def test_parent_summary_is_exact_low_sensitivity_contract(status, reason, accepted) -> None:
    body = json.dumps(_summary(status=status, reason=reason))
    assert (candidate_script._validated_summary(body) is not None) is accepted
    assert candidate_script._validated_summary(body + "private") is None
    assert candidate_script._validated_summary("x" * 4097) is None


def test_parent_uses_fixed_guard_and_only_pass_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded: dict[str, object] = {}

    def guard(module: str, **kwargs: object) -> tuple[str, None]:
        recorded["module"] = module
        recorded.update(kwargs)
        return json.dumps(_summary()), None

    monkeypatch.setattr(candidate_script, "guarded_worker", guard)
    assert (
        candidate_script.main(["--evidence-root", str(tmp_path), "--report-root", str(tmp_path)])
        == 0
    )
    assert recorded["module"] == "scripts.run_many_threads_soak_candidate"
    assert recorded["arguments"] == (
        "--evidence-root",
        str(tmp_path),
        "--report-root",
        str(tmp_path),
    )
    assert recorded["timeout_seconds"] == 1200
    assert recorded["error_prefix"] == "多Thread复验失败"
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"

    monkeypatch.setattr(
        candidate_script,
        "guarded_worker",
        lambda *_a, **_k: (json.dumps(_summary(status="FAIL", reason="limit_exceeded")), None),
    )
    assert (
        candidate_script.main(["--evidence-root", str(tmp_path), "--report-root", str(tmp_path)])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "FAIL"

    monkeypatch.setattr(
        candidate_script, "guarded_worker", lambda *_a, **_k: (None, "soak_worker_timeout")
    )
    assert (
        candidate_script.main(["--evidence-root", str(tmp_path), "--report-root", str(tmp_path)])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "多Thread复验失败：soak_worker_timeout\n"


def test_missing_or_ambiguous_profile_fails_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(candidate_script, "_ARCHIVE", tmp_path)
    with pytest.raises(KernelError) as error:
        candidate_script._only_profile("macos")
    assert error.value.code == "soak_profile_invalid"
    root = tmp_path / "profiles" / "macos"
    root.mkdir(parents=True)
    (root / ("a" * 32)).mkdir()
    (root / ("b" * 32)).mkdir()
    with pytest.raises(KernelError) as error:
        candidate_script._only_profile("macos")
    assert error.value.code == "soak_profile_invalid"
    assert not (tmp_path / "attempts").exists()


def test_worker_redacts_private_failure_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(_evidence: Path, _report: Path) -> dict[str, str]:
        raise KernelError("soak_profile_invalid", f"private={tmp_path}/secret")

    monkeypatch.setattr(candidate_script, "run_candidate", fail)
    assert (
        candidate_script.main(
            ["--internal-worker", "--evidence-root", str(tmp_path), "--report-root", str(tmp_path)]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "多Thread复验失败：soak_profile_invalid\n"


async def test_candidate_reuses_sealed_baseline_and_publishes_independent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = "macos"
    archive = tmp_path / "archive"
    monkeypatch.setattr(candidate_script, "_ARCHIVE", archive)
    monkeypatch.setattr(
        candidate_script, "read_environment", lambda: SimpleNamespace(platform=platform)
    )
    monkeypatch.setattr(candidate_script, "_revision", lambda: REVISION)
    baseline_directory, baseline = _run(archive / "raw" / platform, latency=100)
    from scripts.soak_evidence import read_published_run

    _, digest = read_published_run(baseline_directory)
    profile = _profile(baseline, digest)
    profile_directory, profile_sha = publish_profile(
        archive / "profiles" / platform, profile, baseline_directory
    )
    assert read_profile(profile_directory) == (profile, profile_sha)
    evidence_root = tmp_path / "candidate"
    candidate_directory, measured = _run(
        evidence_root,
        latency=105,
        profile_ref=SoakProfileReference(profile_id=profile.profile_id, sha256=profile_sha),
    )
    called: dict[str, object] = {}

    async def run(_root: Path, **kwargs: object):
        called.update(kwargs)
        return candidate_directory, measured

    monkeypatch.setattr(candidate_script, "run_many_threads", run)
    summary = await candidate_script.run_candidate(evidence_root, tmp_path / "reports")
    assert summary["status"] == "PASS" and summary["reason"] == "within_limits"
    assert summary["baseline_run_id"] == baseline.run_id
    assert summary["candidate_run_id"] == measured.run_id
    assert called == {
        "code_revision": REVISION,
        "thread_count": 500,
        "list_limit": 50,
        "restart_count": 3,
        "startup_timeout_seconds": 120,
        "page_timeout_seconds": 120,
        "threshold_profile_ref": SoakProfileReference(
            profile_id=profile.profile_id, sha256=profile_sha
        ),
    }


async def test_candidate_invalid_baseline_never_starts_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "archive"
    monkeypatch.setattr(candidate_script, "_ARCHIVE", archive)
    monkeypatch.setattr(
        candidate_script, "read_environment", lambda: SimpleNamespace(platform="macos")
    )
    baseline_directory, baseline = _run(archive / "raw" / "macos", latency=100, legacy=True)
    from scripts.soak_evidence import read_published_run

    _, digest = read_published_run(baseline_directory)
    profile = _profile(baseline, digest)
    # 旧v1不可能正常封印；此处构造预检后的错误绑定，确认Runner不启动。
    profile_root = archive / "profiles" / "macos" / profile.profile_id
    profile_root.mkdir(parents=True)
    monkeypatch.setattr(candidate_script, "read_profile", lambda _path: (profile, "a" * 64))
    monkeypatch.setattr(
        candidate_script, "run_many_threads", lambda *_a, **_k: pytest.fail("不应启动负载")
    )
    with pytest.raises(KernelError) as error:
        await candidate_script.run_candidate(tmp_path / "evidence", tmp_path / "reports")
    assert error.value.code == "soak_profile_baseline_invalid"
    assert not (tmp_path / "evidence").exists()
