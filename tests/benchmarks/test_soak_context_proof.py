from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from scripts.soak_context_proof import (
    CONTEXT_PROOF_FILENAME,
    SoakContextProof,
    SoakContextTurn,
    SoakEventMarker,
)
from scripts.soak_evidence import publish_run, read_published_run
from scripts.soak_manifest import SoakManifestV2
from tests.benchmarks.test_soak_evidence import _input


def _proof(run_id: str) -> SoakContextProof:
    return SoakContextProof(
        spec_version="harnessix.soak-context-proof/v1",
        run_id=run_id,
        turns=(
            SoakContextTurn(
                ordinal=1,
                phase="measure",
                model_steps=1,
                context_inspections=1,
                history_inspections=1,
                compaction_plans=1,
                summary_attempts=1,
                completed_summaries=1,
                window_activations=1,
            ),
        ),
        events=tuple(
            SoakEventMarker(sequence=index, turn_ordinal=0 if index == 1 else 1, kind=kind)
            for index, kind in enumerate(
                (
                    "thread_created",
                    "turn_started",
                    "compaction_planned",
                    "compaction_attempt_started",
                    "compaction_usage_observed",
                    "compaction_attempt_finished",
                    "compaction_summarized",
                    "compaction_window_activated",
                    "model_history_prepared",
                    "context_prepared",
                ),
                start=1,
            )
        ),
        final_window_count=1,
        final_event_sequence=10,
    )


def _v2():
    v1, samples = _input()
    proof = _proof(v1.run_id)
    digest = sha256((proof.model_dump_json() + "\n").encode()).hexdigest()
    data = v1.model_dump(mode="json")
    data.update(
        spec_version="harnessix.soak-manifest/v2",
        scenario_version="harnessix.soak-scenario/v2",
        summary_request_count=1,
        context_proof_sha256=digest,
        evidence_sha256={**v1.evidence_sha256, CONTEXT_PROOF_FILENAME: digest},
    )
    return SoakManifestV2.model_validate(data), samples, proof


def test_v2_proof_is_canonical_and_v1_reader_stays_compatible(tmp_path) -> None:
    v1, samples = _input()
    old_directory, _ = publish_run(tmp_path / "old", v1, samples)
    assert read_published_run(old_directory)[0] == v1
    assert not (old_directory / CONTEXT_PROOF_FILENAME).exists()

    manifest, samples, proof = _v2()
    directory, _ = publish_run(tmp_path / "new", manifest, samples, context_proof=proof)
    restored, _ = read_published_run(directory)
    assert restored == manifest
    assert isinstance(restored, SoakManifestV2)
    assert (directory / CONTEXT_PROOF_FILENAME).read_bytes() == (
        proof.model_dump_json() + "\n"
    ).encode()


def test_missing_proof_rejected_before_run_directory_creation(tmp_path) -> None:
    manifest, samples, _ = _v2()
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        publish_run(root, manifest, samples)
    assert caught.value.code == "soak_context_proof_invalid"
    assert not root.exists()


@pytest.mark.parametrize("mutation", ["body", "extra_file", "removed_file"])
def test_reader_rejects_tampered_v2_proof(tmp_path, mutation) -> None:
    manifest, samples, proof = _v2()
    directory, _ = publish_run(tmp_path / "evidence", manifest, samples, context_proof=proof)
    target = directory / CONTEXT_PROOF_FILENAME
    if mutation == "body":
        target.write_bytes(
            target.read_bytes().replace(b'"final_window_count":1', b'"final_window_count":2')
        )
    elif mutation == "extra_file":
        (directory / "secret.txt").write_text("unexpected")
    else:
        target.unlink()
    with pytest.raises(KernelError) as caught:
        read_published_run(directory)
    assert caught.value.code == "soak_run_invalid"


def test_proof_rejects_ordinal_and_incomplete_compaction() -> None:
    proof = _proof("a" * 32)
    with pytest.raises(ValidationError):
        SoakContextProof.model_validate(
            {**proof.model_dump(), "turns": [{**proof.turns[0].model_dump(), "ordinal": 2}]}
        )
    with pytest.raises(ValidationError):
        SoakContextTurn.model_validate({**proof.turns[0].model_dump(), "summary_attempts": 0})
    markers = [marker.model_dump() for marker in proof.events]
    markers[4]["kind"] = "compaction_summarized"
    with pytest.raises(ValidationError):
        SoakContextProof.model_validate({**proof.model_dump(), "events": markers})
    markers = [marker.model_dump() for marker in proof.events]
    markers[4]["sequence"] = 999
    with pytest.raises(ValidationError):
        SoakContextProof.model_validate({**proof.model_dump(), "events": markers})
