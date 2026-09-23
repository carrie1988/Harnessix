from __future__ import annotations

import asyncio
import json
from hashlib import sha256

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.service import AgentApplicationService
from harnessix.protocol.contracts import ThreadListResult
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import COMMIT_FILENAME, MANIFEST_FILENAME, read_published_run
from scripts.soak_manifest import SoakManifestV6, SoakProfileReference
from scripts.soak_many_threads import run_many_threads
from scripts.soak_sample_file import read_sample_file
from scripts.soak_thread_proof import THREAD_PROOF_FILENAME, SoakThreadProof


def _assert_failed_attempt(root, *, phase: str) -> None:
    attempt_directory = next((root / "attempts").iterdir())
    _, final = read_attempt(attempt_directory)
    assert final is not None and final.outcome == "failed" and final.phase == phase
    assert not (root / attempt_directory.name).exists()


async def test_many_threads_smoke_restarts_runtime_and_reads_every_page(tmp_path) -> None:
    run_directory, manifest = await run_many_threads(
        tmp_path / "evidence",
        code_revision="a" * 40,
        thread_count=12,
        list_limit=5,
        restart_count=2,
    )

    restored, _ = read_published_run(run_directory)
    samples, _, statistics = read_sample_file(
        run_directory,
        expected_sha256=manifest.evidence_sha256["samples.jsonl"],
        run_id=manifest.run_id,
        scenario_id="many_threads",
        expected_measured=manifest.sample_counts,
    )
    assert restored == manifest
    assert manifest.spec_version == "harnessix.soak-manifest/v6"
    proof = SoakThreadProof.model_validate_json(
        (run_directory / THREAD_PROOF_FILENAME).read_bytes()
    )
    assert len(proof.cycles) == 3
    assert all(len(cycle.pages) == 3 for cycle in proof.cycles)
    assert len({tag for page in proof.cycles[0].pages for tag in page.thread_tags}) == 12
    _, attempt_final = read_attempt(tmp_path / "evidence" / "attempts" / manifest.run_id)
    assert attempt_final is not None and attempt_final.outcome == "committed"
    assert manifest.status == "unverified"
    assert manifest.load.thread_count == 12
    assert manifest.load.warmup_count == 4
    assert manifest.sample_counts == {
        "app_service_startup": 2,
        "thread_list_page": 6,
        "rss_peak": 1,
    }
    assert len([sample for sample in samples if sample.phase == "warmup"]) == 4
    assert statistics == manifest.statistics
    assert manifest.provider.request_count == 0
    assert manifest.file_watermarks.db_after_bytes > manifest.file_watermarks.db_before_bytes
    raw = b"".join(path.read_bytes() for path in run_directory.iterdir())
    assert str(tmp_path).encode() not in raw
    assert b"session.db" not in raw


async def test_thread_proof_tamper_rejected_even_after_resealing_manifest(tmp_path) -> None:
    run_directory, _ = await run_many_threads(
        tmp_path / "evidence", code_revision="a" * 40, thread_count=12, list_limit=5
    )
    proof_path = run_directory / THREAD_PROOF_FILENAME
    proof = SoakThreadProof.model_validate_json(proof_path.read_bytes())
    altered = proof.model_dump(mode="json")
    altered["cycles"][1]["pages"][0]["sample_index"] += 1
    proof_body = (SoakThreadProof.model_validate(altered).model_dump_json() + "\n").encode()
    proof_path.write_bytes(proof_body)
    manifest_path = run_directory / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_bytes())
    digest = sha256(proof_body).hexdigest()
    manifest["thread_proof_sha256"] = digest
    manifest["evidence_sha256"][THREAD_PROOF_FILENAME] = digest
    manifest_body = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    manifest_path.write_bytes(manifest_body)
    commit_path = run_directory / COMMIT_FILENAME
    commit = json.loads(commit_path.read_bytes())
    commit["manifest_sha256"] = sha256(manifest_body).hexdigest()
    commit_path.write_bytes((json.dumps(commit, separators=(",", ":")) + "\n").encode())
    with pytest.raises(KernelError) as error:
        read_published_run(run_directory)
    assert error.value.code == "soak_run_invalid"


@pytest.mark.parametrize("tamper", ["duplicate_tag", "missing_page", "wrong_next", "wrong_set"])
async def test_thread_proof_rejects_broken_page_coverage(tmp_path, tamper) -> None:
    run_directory, _ = await run_many_threads(
        tmp_path / "evidence", code_revision="a" * 40, thread_count=12, list_limit=5
    )
    proof = SoakThreadProof.model_validate_json(
        (run_directory / THREAD_PROOF_FILENAME).read_bytes()
    ).model_dump(mode="json")
    pages = proof["cycles"][1]["pages"]
    if tamper == "duplicate_tag":
        pages[1]["thread_tags"][0] = pages[0]["thread_tags"][0]
    elif tamper == "missing_page":
        pages.pop()
    elif tamper == "wrong_next":
        pages[0]["has_next"] = False
    else:
        pages[0]["thread_tags"][0] = "0" * 64
    with pytest.raises(ValidationError):
        SoakThreadProof.model_validate(proof)


async def test_baseline_requires_three_measured_restarts(tmp_path) -> None:
    _, smoke = await run_many_threads(
        tmp_path / "evidence",
        code_revision="b" * 40,
        thread_count=4,
        list_limit=2,
        restart_count=2,
    )
    data = smoke.model_dump()
    data["status"] = "baseline"
    data["load"]["thread_count"] = 500
    with pytest.raises(ValidationError, match="多Thread启动或列表样本数不足"):
        SoakManifestV6.model_validate(data)


async def test_profile_bound_thread_candidate_requires_formal_load(tmp_path) -> None:
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_many_threads(
            root,
            code_revision="a" * 40,
            thread_count=12,
            threshold_profile_ref=SoakProfileReference(profile_id="a" * 32, sha256="b" * 64),
        )
    assert error.value.code == "soak_load_invalid"
    assert not root.exists()


@pytest.mark.parametrize(
    ("thread_count", "list_limit", "restart_count"),
    [(0, 50, 3), (1, 201, 3), (1, 50, 0), (500, 50, 2)],
)
async def test_invalid_many_thread_plan_does_not_create_evidence(
    tmp_path, thread_count, list_limit, restart_count
) -> None:
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_many_threads(
            root,
            code_revision="c" * 40,
            thread_count=thread_count,
            list_limit=list_limit,
            restart_count=restart_count,
        )
    assert error.value.code == "soak_load_invalid"
    assert not root.exists()


async def test_empty_page_and_timeout_fail_without_published_run(tmp_path, monkeypatch) -> None:
    async def empty(self, params):
        return ThreadListResult(threads=(), next_cursor=None)

    root = tmp_path / "empty-evidence"
    monkeypatch.setattr(AgentApplicationService, "list_threads", empty)
    with pytest.raises(KernelError) as error:
        await run_many_threads(root, code_revision="d" * 40, thread_count=2)
    assert error.value.code == "soak_list_invalid"
    _assert_failed_attempt(root, phase="warming")

    async def delayed(self, params):
        await asyncio.sleep(1)

    monkeypatch.setattr(AgentApplicationService, "list_threads", delayed)
    root = tmp_path / "timeout-evidence"
    with pytest.raises(KernelError) as error:
        await run_many_threads(
            root,
            code_revision="d" * 40,
            thread_count=2,
            page_timeout_seconds=0.01,
        )
    assert error.value.code == "soak_list_timeout"
    _assert_failed_attempt(root, phase="warming")


async def test_startup_deadline_drains_open_runtime_before_temp_cleanup(
    tmp_path, monkeypatch
) -> None:
    enter = AgentRuntime.__aenter__

    async def delayed_enter(self):
        await asyncio.sleep(0.05)
        return await enter(self)

    monkeypatch.setattr(AgentRuntime, "__aenter__", delayed_enter)
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as error:
        await run_many_threads(
            root,
            code_revision="e" * 40,
            thread_count=2,
            startup_timeout_seconds=0.01,
        )
    assert error.value.code == "soak_startup_timeout"
    _assert_failed_attempt(root, phase="warming")
