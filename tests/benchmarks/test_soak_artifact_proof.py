from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES
from scripts.soak_artifact_proof import (
    ARTIFACT_PROOF_FILENAME,
    SoakArtifactCleanup,
    SoakArtifactEntry,
    SoakArtifactProof,
)
from scripts.soak_environment import SoakEnvironment
from scripts.soak_evidence import publish_run, read_published_run
from scripts.soak_manifest import SoakManifest, SoakManifestV3
from scripts.soak_provider import SoakProvider
from scripts.soak_rss import RssObservation
from scripts.soak_run_common import publish_measured_run
from scripts.soak_sample_file import sample_sha256
from scripts.soak_samples import SoakSample, validate_sample_series
from tests.benchmarks.test_soak_manifest import RUN_ID, _manifest_data


def _v3() -> tuple[SoakManifestV3, tuple[SoakSample, ...], SoakArtifactProof]:
    near_bytes = MAX_ARTIFACT_BYTES * 9 // 10
    proof = SoakArtifactProof(
        spec_version="harnessix.soak-artifact-proof/v1",
        run_id=RUN_ID,
        entries=(
            SoakArtifactEntry(
                ordinal=1,
                phase="warmup",
                size_class="small",
                size_bytes=100,
                record_count=1,
                read_record_count=1,
                page_count=1,
                publish_sample_index=1,
                read_sample_indices=(2,),
            ),
            SoakArtifactEntry(
                ordinal=2,
                phase="measure",
                size_class="near_limit",
                size_bytes=near_bytes,
                record_count=2,
                read_record_count=2,
                page_count=1,
                publish_sample_index=3,
                read_sample_indices=(4,),
            ),
        ),
        cleanup=SoakArtifactCleanup(
            before_body_bytes=100 + near_bytes,
            after_body_bytes=0,
            expired_count=2,
            tombstone_count=2,
            manifest_count=2,
            protected_count=0,
        ),
    )

    def latency(index: int, phase: str, metric: str) -> SoakSample:
        return SoakSample.model_validate(
            {
                "spec_version": "harnessix.soak-sample/v1",
                "run_id": RUN_ID,
                "scenario_id": "artifact_growth",
                "sample_index": index,
                "phase": phase,
                "metric": metric,
                "value": 100,
                "unit": "ns",
                "clock": "monotonic_ns",
            }
        )

    samples = (
        latency(1, "warmup", "artifact_publish"),
        latency(2, "warmup", "artifact_read"),
        latency(3, "measure", "artifact_publish"),
        latency(4, "measure", "artifact_read"),
        SoakSample(
            spec_version="harnessix.soak-sample/v1",
            run_id=RUN_ID,
            scenario_id="artifact_growth",
            sample_index=5,
            phase="measure",
            metric="rss_peak",
            value=4096,
            unit="bytes",
            rss_source="getrusage",
            rss_raw_unit="KiB",
            rss_normalization="kib_times_1024",
            rss_raw_value=4,
            rss_bytes=4096,
        ),
    )
    counts = {"artifact_publish": 1, "artifact_read": 1, "rss_peak": 1}
    proof_digest = sha256((proof.model_dump_json() + "\n").encode()).hexdigest()
    sample_digest = sample_sha256(samples)
    data = _manifest_data(
        sample_digest,
        validate_sample_series(
            samples, run_id=RUN_ID, scenario_id="artifact_growth", expected_measured=counts
        ),
    )
    data.update(
        spec_version="harnessix.soak-manifest/v3",
        scenario_version="harnessix.soak-scenario/v3",
        scenario_id="artifact_growth",
        measurement_boundary="artifact_store",
        provider={**data["provider"], "request_count": 4},
        load={"turn_count": 2, "thread_count": 1, "artifact_count": 2, "warmup_count": 1},
        sample_counts=counts,
        file_watermarks={
            **data["file_watermarks"],
            "artifact_after_bytes": proof.cleanup.before_body_bytes,
        },
        evidence_sha256={"samples.jsonl": sample_digest, ARTIFACT_PROOF_FILENAME: proof_digest},
        artifact_proof_sha256=proof_digest,
    )
    return SoakManifestV3.model_validate(data), samples, proof


def test_v3_round_trip_binds_proof_samples_and_manifest(tmp_path) -> None:
    manifest, samples, proof = _v3()
    directory, digest = publish_run(tmp_path / "evidence", manifest, samples, artifact_proof=proof)
    assert read_published_run(directory) == (manifest, digest)
    assert (directory / ARTIFACT_PROOF_FILENAME).read_bytes() == (
        proof.model_dump_json() + "\n"
    ).encode()
    with pytest.raises(ValidationError):
        SoakManifest.model_validate(
            {**manifest.model_dump(mode="json"), "spec_version": "harnessix.soak-manifest/v1"}
        )


def test_v3_measured_run_builder_publishes_without_v1_intermediate(tmp_path) -> None:
    manifest, samples, proof = _v3()
    provider = SoakProvider()
    provider.request_count = 4
    directory, built = publish_measured_run(
        tmp_path / "evidence",
        run_id=manifest.run_id,
        code_revision=manifest.code_revision,
        scenario_id="artifact_growth",
        seed=manifest.seed,
        environment=SoakEnvironment(
            platform="linux",
            python_version="3.12.10",
            cpu_count=4,
            physical_memory_bytes=8 * 1024**3,
            hardware_class="c4-m8",
        ),
        started_at=manifest.started_at,
        load=manifest.load,
        samples=samples,
        provider=provider,
        rss=RssObservation(
            source="getrusage",
            raw_value=4,
            raw_unit="KiB",
            normalization="kib_times_1024",
            rss_bytes=4096,
            unit_verified=True,
        ),
        file_watermarks=manifest.file_watermarks,
        baseline=False,
        artifact_proof=proof,
    )
    assert isinstance(built, SoakManifestV3)
    assert read_published_run(directory)[0] == built


def test_v3_missing_or_tampered_proof_is_not_published(tmp_path) -> None:
    manifest, samples, proof = _v3()
    root = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        publish_run(root, manifest, samples)
    assert caught.value.code == "soak_artifact_proof_invalid"
    assert not root.exists()
    directory, _ = publish_run(root, manifest, samples, artifact_proof=proof)
    path = directory / ARTIFACT_PROOF_FILENAME
    path.write_bytes(path.read_bytes().replace(b'"size_bytes":100', b'"size_bytes":101'))
    with pytest.raises(KernelError) as caught:
        read_published_run(directory)
    assert caught.value.code == "soak_run_invalid"
    path.write_bytes(b"\xff\n")
    with pytest.raises(KernelError) as caught:
        read_published_run(directory)
    assert caught.value.code == "soak_run_invalid"


def test_v3_baseline_requires_frozen_load_and_page_count() -> None:
    manifest, _, proof = _v3()
    with pytest.raises(ValidationError, match="2件预热"):
        SoakManifestV3.model_validate({**manifest.model_dump(mode="json"), "status": "baseline"})
    data = proof.model_dump(mode="json")
    data["entries"][1]["page_count"] = 2
    with pytest.raises(ValidationError, match="分页样本数量"):
        SoakArtifactProof.model_validate(data)


@pytest.mark.parametrize("point", ["after_samples", "after_manifest", "before_commit"])
def test_v3_interruption_is_not_a_published_run(tmp_path, point) -> None:
    manifest, samples, proof = _v3()

    def interrupt(current: str) -> None:
        if current == point:
            raise RuntimeError("预期的提交中断")

    with pytest.raises(RuntimeError, match="预期的提交中断"):
        publish_run(tmp_path / "evidence", manifest, samples, artifact_proof=proof, fault=interrupt)
    with pytest.raises(KernelError) as caught:
        read_published_run(tmp_path / "evidence" / manifest.run_id)
    assert caught.value.code == "soak_run_invalid"


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "cleanup", "phase"])
def test_v3_rejects_inconsistent_coverage(tmp_path, mutation) -> None:
    manifest, samples, proof = _v3()
    data = proof.model_dump(mode="json")
    if mutation == "duplicate":
        data["entries"][1]["read_sample_indices"] = [2]
    elif mutation == "missing":
        data["entries"][1]["read_sample_indices"] = [5]
    elif mutation == "cleanup":
        data["cleanup"]["after_body_bytes"] = 1
    else:
        data["entries"][1]["phase"] = "warmup"
    if mutation == "cleanup":
        with pytest.raises(ValidationError):
            SoakArtifactProof.model_validate(data)
    else:
        bad_proof = SoakArtifactProof.model_validate(data)
        with pytest.raises(KernelError) as caught:
            publish_run(tmp_path / mutation, manifest, samples, artifact_proof=bad_proof)
        assert caught.value.code == "soak_artifact_proof_invalid"
