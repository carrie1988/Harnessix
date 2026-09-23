from __future__ import annotations

import asyncio

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_long_session import run_long_session
from scripts.soak_sample_file import read_sample_file


async def test_long_session_smoke_uses_runtime_replay_and_publishes_only_numbers(tmp_path) -> None:
    run_directory, manifest = await run_long_session(
        tmp_path / "evidence",
        code_revision="a" * 40,
        turn_count=3,
        warmup_count=2,
        seed=7,
    )

    restored, _ = read_published_run(run_directory)
    samples, _, statistics = read_sample_file(
        run_directory,
        expected_sha256=manifest.evidence_sha256["samples.jsonl"],
        run_id=manifest.run_id,
        scenario_id="long_session",
        expected_measured=manifest.sample_counts,
    )
    assert restored == manifest
    _, attempt_final = read_attempt(tmp_path / "evidence" / "attempts" / manifest.run_id)
    assert attempt_final is not None and attempt_final.outcome == "committed"
    assert manifest.status == "unverified"
    assert manifest.load.turn_count == 3
    assert manifest.load.warmup_count == 2
    assert manifest.provider.request_count == 5
    assert manifest.sample_counts == {"turn_local": 3, "rss_peak": 1}
    assert [sample.phase for sample in samples] == [
        "warmup",
        "warmup",
        "measure",
        "measure",
        "measure",
        "measure",
    ]
    assert statistics == manifest.statistics
    assert manifest.file_watermarks.db_after_bytes > 0
    assert not (tmp_path / "session.db").exists()
    raw = b"".join(path.read_bytes() for path in run_directory.iterdir())
    assert "固定Soak输入".encode() not in raw
    assert b"soak-complete" not in raw
    assert b"session.db" not in raw


@pytest.mark.parametrize(
    ("revision", "turn_count", "warmup_count"),
    [("A" * 40, 1, 0), ("a" * 40, 0, 0), ("a" * 40, True, 0), ("a" * 40, 1, -1)],
)
async def test_invalid_plan_fails_before_evidence_directory_creation(
    tmp_path, revision, turn_count, warmup_count
) -> None:
    evidence_root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_long_session(
            evidence_root,
            code_revision=revision,
            turn_count=turn_count,
            warmup_count=warmup_count,
        )
    assert error.value.code == "soak_load_invalid"
    assert not evidence_root.exists()


async def test_timeout_does_not_publish_partial_run(tmp_path, monkeypatch) -> None:
    async def delayed_turn(self, thread_id, prompt, *, request_id):
        await asyncio.sleep(1)

    monkeypatch.setattr(AgentRuntime, "run_turn", delayed_turn)
    evidence_root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_long_session(
            evidence_root,
            code_revision="b" * 40,
            turn_count=1,
            warmup_count=0,
            turn_timeout_seconds=0.01,
        )
    assert error.value.code == "soak_turn_timeout"
    attempt_directory = next((evidence_root / "attempts").iterdir())
    _, final = read_attempt(attempt_directory)
    assert final is not None and final.outcome == "failed" and final.phase == "measuring"
    assert not (evidence_root / attempt_directory.name).exists()


async def test_environment_preflight_failure_persists_failed_attempt(tmp_path, monkeypatch) -> None:
    def unavailable():
        raise KernelError("soak_environment_unavailable", "环境信息不可用")

    monkeypatch.setattr("scripts.soak_long_session.read_environment", unavailable)
    evidence_root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_long_session(
            evidence_root,
            code_revision="c" * 40,
            turn_count=1,
            warmup_count=0,
        )
    assert error.value.code == "soak_environment_unavailable"
    attempt_directory = next((evidence_root / "attempts").iterdir())
    _, final = read_attempt(attempt_directory)
    assert final is not None and final.outcome == "failed" and final.phase == "prepared"


async def test_formal_baseline_rejects_unverified_revision_before_work(tmp_path) -> None:
    with pytest.raises(KernelError) as error:
        await run_long_session(
            tmp_path / "evidence",
            code_revision="f" * 40,
            turn_count=1000,
            warmup_count=0,
        )
    assert error.value.code == "soak_revision_invalid"
    assert not (tmp_path / "evidence").exists()
