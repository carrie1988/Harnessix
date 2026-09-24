from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from scripts.soak_artifact_growth import (
    MeasuredArtifactStore,
    SoakArtifactProvider,
    _body_counts,
    run_artifact_growth,
)
from scripts.soak_artifact_proof import ARTIFACT_PROOF_FILENAME, SoakArtifactProof
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


def test_artifact_readonly_probe_closes_sqlite_handle(tmp_path, monkeypatch) -> None:
    database = tmp_path / "session.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE agent_artifacts (body BLOB, state TEXT)")
        connection.commit()
    original = sqlite3.connect
    opened = []

    def track(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr("scripts.soak_artifact_growth.sqlite3.connect", track)
    assert _body_counts(database) == (0, 0, 0)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


async def test_real_agent_artifact_soak_reads_every_page_and_cleans_body(tmp_path) -> None:
    evidence = tmp_path / "evidence"
    directory, manifest = await run_artifact_growth(
        evidence, code_revision=REVISION, turn_count=2, warmup_count=1
    )
    assert manifest.status == "unverified"
    assert manifest.load.artifact_count == 3
    assert manifest.sample_counts["artifact_publish"] == 2
    assert manifest.sample_counts["artifact_read"] > 2
    assert read_published_run(directory)[0] == manifest
    assert read_attempt(evidence / "attempts" / manifest.run_id)[1].outcome == "committed"
    proof = SoakArtifactProof.model_validate_json(
        (directory / ARTIFACT_PROOF_FILENAME).read_bytes()
    )
    assert [entry.size_class for entry in proof.entries] == ["small", "near_limit", "small"]
    assert proof.cleanup.after_body_bytes == 0
    assert proof.cleanup.tombstone_count == 3
    assert all(entry.read_record_count == entry.record_count for entry in proof.entries)
    assert proof.entries[1].page_count > 1
    body = b"".join(path.read_bytes() for path in directory.iterdir())
    assert b"needle" not in body and str(tmp_path).encode() not in body


@pytest.mark.parametrize("turn_count,warmup", [(0, 0), (101, 0), (1, 3), (20, 1)])
async def test_invalid_artifact_load_does_not_begin_attempt(
    tmp_path, turn_count: int, warmup: int
) -> None:
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_artifact_growth(
            evidence, code_revision=REVISION, turn_count=turn_count, warmup_count=warmup
        )
    assert caught.value.code == "soak_load_invalid"
    assert not evidence.exists()


async def test_artifact_turn_timeout_records_failed_attempt(tmp_path, monkeypatch) -> None:
    async def stalled(self, request, cancel):
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(SoakArtifactProvider, "stream", stalled)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_artifact_growth(
            evidence,
            code_revision=REVISION,
            turn_count=1,
            warmup_count=0,
            turn_timeout_seconds=0.01,
        )
    assert caught.value.code == "soak_artifact_timeout"
    attempts = tuple((evidence / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_artifact_page_timeout_drains_sqlite_before_cleanup(tmp_path, monkeypatch) -> None:
    original = MeasuredArtifactStore.read

    async def delayed(self, *args, **kwargs):
        await asyncio.sleep(0.03)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(MeasuredArtifactStore, "read", delayed)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_artifact_growth(
            evidence,
            code_revision=REVISION,
            turn_count=1,
            warmup_count=0,
            page_timeout_seconds=0.01,
        )
    assert caught.value.code == "soak_artifact_timeout"
    attempts = tuple((evidence / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_artifact_page_corruption_cannot_publish_run(tmp_path, monkeypatch) -> None:
    original = MeasuredArtifactStore.read

    async def corrupted(self, *args, **kwargs):
        page = await original(self, *args, **kwargs)
        return page.model_copy(update={"offset": page.offset + 1})

    monkeypatch.setattr(MeasuredArtifactStore, "read", corrupted)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_artifact_growth(evidence, code_revision=REVISION, turn_count=1, warmup_count=0)
    assert caught.value.code == "soak_artifact_page_invalid"
    attempts = tuple((evidence / "attempts").iterdir())
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_formal_artifact_run_rejects_unverified_revision_before_attempt(tmp_path) -> None:
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_artifact_growth(evidence, code_revision=REVISION, turn_count=20, warmup_count=2)
    assert caught.value.code == "soak_revision_invalid"
    assert not evidence.exists()


async def test_artifact_cleanup_mismatch_is_failed_attempt(tmp_path, monkeypatch) -> None:
    original = MeasuredArtifactStore.collect

    async def incomplete(self, *args, **kwargs):
        report = await original(self, *args, **kwargs)
        return report.__class__(
            examined=report.examined,
            expired=0,
            protected=report.protected,
            collected_at=report.collected_at,
            next_after=report.next_after,
        )

    monkeypatch.setattr(MeasuredArtifactStore, "collect", incomplete)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_artifact_growth(evidence, code_revision=REVISION, turn_count=1, warmup_count=0)
    assert caught.value.code == "soak_artifact_cleanup_invalid"
    attempts = tuple((evidence / "attempts").iterdir())
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_artifact_formal_contract_connects_to_frozen_threshold(tmp_path, monkeypatch) -> None:
    # 这里只校验合同与双Run链；真实发行基线仍由Runner在干净Revision执行。
    monkeypatch.setattr("scripts.soak_artifact_growth.check_release_revision", lambda _: None)
    baseline_dir, baseline = await run_artifact_growth(
        tmp_path / "baseline", code_revision=REVISION, turn_count=20, warmup_count=2
    )
    assert baseline.status == "baseline"
    _, baseline_digest = read_published_run(baseline_dir)
    initial = _profile(baseline, baseline_digest, margin=10000)
    data = initial.model_dump(mode="json")
    watermarks = baseline.file_watermarks
    for kind in ("db", "wal", "artifact"):
        before = getattr(watermarks, f"{kind}_before_bytes")
        after = getattr(watermarks, f"{kind}_after_bytes")
        data["growth_limits"][kind]["upper_bytes"] = max(0, after - before) * 2
    profile = SoakThresholdProfile.model_validate(data)
    profile_dir, profile_sha = publish_profile(tmp_path / "profiles", profile, baseline_dir)
    candidate_dir, candidate = await run_artifact_growth(
        tmp_path / "candidate",
        code_revision=REVISION,
        turn_count=20,
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


async def test_artifact_cancel_records_failed_attempt(tmp_path, monkeypatch) -> None:
    entered = asyncio.Event()

    async def stalled(self, request, cancel):
        entered.set()
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(SoakArtifactProvider, "stream", stalled)
    evidence = tmp_path / "evidence"
    task = asyncio.create_task(
        run_artifact_growth(evidence, code_revision=REVISION, turn_count=1, warmup_count=0)
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempts = tuple((evidence / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()
