"""Action恢复固定故障矩阵Runner的真实主链回归。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from scripts import soak_action_recovery
from scripts.soak_action_proof import ACTION_PROOF_FILENAME, SoakActionProof
from scripts.soak_action_recovery import UnknownOutcomeExecutor, run_action_recovery
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakProfileReference
from scripts.soak_threshold import (
    SoakThresholdProfile,
    publish_profile,
    read_report,
    verify_and_publish,
)
from tests.benchmarks.test_soak_threshold import _profile

REVISION = "a" * 40


async def test_real_action_fault_matrix_soak_publishes_v7_evidence(tmp_path) -> None:
    evidence = tmp_path / "evidence"
    directory, manifest = await run_action_recovery(
        evidence, code_revision=REVISION, cycle_count=2, warmup_count=1
    )
    assert manifest.status == "unverified"
    assert manifest.spec_version == "harnessix.soak-manifest/v7"
    assert manifest.load.fault_matrix_version == "action-recovery-v1"
    assert manifest.sample_counts == {"recovery_scan": 2, "rss_peak": 1}
    assert manifest.fault_counts.unknown_effect == 6
    assert manifest.fault_counts.duplicate_effect == 0
    assert manifest.fault_counts.orphan == 0
    assert read_published_run(directory)[0] == manifest
    assert read_attempt(evidence / "attempts" / manifest.run_id)[1].outcome == "committed"
    proof = SoakActionProof.model_validate_json((directory / ACTION_PROOF_FILENAME).read_bytes())
    assert len(proof.cycles) == 3
    assert [cycle.phase for cycle in proof.cycles] == ["warmup", "measure", "measure"]
    assert [cycle.owner_generation for cycle in proof.cycles] == [1, 2, 3]
    assert proof.unknown_resolved == 6
    assert proof.duplicate_effects == 0
    assert proof.crash_exits == 3
    body = b"".join(path.read_bytes() for path in directory.iterdir())
    assert str(tmp_path).encode() not in body and b"file.txt" not in body


@pytest.mark.parametrize(
    "cycle_count,warmup",
    [(0, 0), (101, 0), (1, 5), (20, 1)],
)
async def test_invalid_action_load_does_not_begin_attempt(
    tmp_path, cycle_count: int, warmup: int
) -> None:
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_action_recovery(
            evidence, code_revision=REVISION, cycle_count=cycle_count, warmup_count=warmup
        )
    assert caught.value.code == "soak_load_invalid"
    assert not evidence.exists()


async def test_formal_action_run_rejects_unverified_revision_before_attempt(tmp_path) -> None:
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_action_recovery(evidence, code_revision=REVISION, cycle_count=20, warmup_count=2)
    assert caught.value.code == "soak_revision_invalid"
    assert not evidence.exists()


async def test_crash_child_contract_failure_records_failed_attempt(tmp_path, monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(soak_action_recovery.asyncio, "to_thread", fake_to_thread)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_action_recovery(evidence, code_revision=REVISION, cycle_count=1, warmup_count=0)
    assert caught.value.code == "soak_action_crash_invalid"
    attempts = tuple((evidence / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_reconcile_must_not_replay_unknown_effect(tmp_path, monkeypatch) -> None:
    original = UnknownOutcomeExecutor.reconcile

    async def replaying(self, plan, arguments):
        outcome = await original(self, plan, arguments)
        self.calls += 1
        return outcome

    monkeypatch.setattr(UnknownOutcomeExecutor, "reconcile", replaying)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_action_recovery(evidence, code_revision=REVISION, cycle_count=1, warmup_count=0)
    assert caught.value.code == "soak_action_fault_invalid"
    attempts = tuple((evidence / "attempts").iterdir())
    assert read_attempt(attempts[0])[1].outcome == "failed"


async def test_scan_timeout_records_failed_attempt(tmp_path, monkeypatch) -> None:
    async def stalled(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(soak_action_recovery, "scan_product_action_recovery", stalled)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_action_recovery(
            evidence,
            code_revision=REVISION,
            cycle_count=1,
            warmup_count=0,
            scan_timeout_seconds=1,
        )
    assert caught.value.code == "soak_action_timeout"
    attempts = tuple((evidence / "attempts").iterdir())
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_action_formal_contract_connects_to_frozen_threshold(tmp_path, monkeypatch) -> None:
    # 这里只校验合同与双Run链；真实发行基线仍由Runner在干净Revision执行。
    monkeypatch.setattr("scripts.soak_action_recovery.check_release_revision", lambda _: None)
    baseline_dir, baseline = await run_action_recovery(
        tmp_path / "baseline", code_revision=REVISION, cycle_count=20, warmup_count=2
    )
    assert baseline.status == "baseline"
    _, baseline_digest = read_published_run(baseline_dir)
    initial = _profile(baseline, baseline_digest, margin=10000)
    data = initial.model_dump(mode="json")
    data["expected_fault_counts"] = baseline.fault_counts.model_dump(mode="json")
    watermarks = baseline.file_watermarks
    for kind in ("db", "wal", "artifact"):
        before = getattr(watermarks, f"{kind}_before_bytes")
        after = getattr(watermarks, f"{kind}_after_bytes")
        data["growth_limits"][kind]["upper_bytes"] = max(0, after - before) * 2
    profile = SoakThresholdProfile.model_validate(data)
    profile_dir, profile_sha = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    candidate_dir, candidate = await run_action_recovery(
        tmp_path / "candidate",
        code_revision=REVISION,
        cycle_count=20,
        warmup_count=2,
        threshold_profile_ref=SoakProfileReference(
            profile_id=profile.profile_id, sha256=profile_sha
        ),
    )
    assert candidate.status == "unverified"
    started, final = read_attempt(candidate_dir.parent / "attempts" / candidate.run_id)
    assert started.threshold_profile_ref == candidate.threshold_profile_ref
    assert final.outcome == "committed"
    report_dir, report = verify_and_publish(
        profile_dir, baseline_dir, candidate_dir, tmp_path / "reports"
    )
    assert report.status in {"PASS", "FAIL"}
    assert read_report(report_dir) == report
