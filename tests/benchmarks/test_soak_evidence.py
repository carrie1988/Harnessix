from __future__ import annotations

import os
from hashlib import sha256

import pytest

from harnessix.agent.errors import KernelError
from scripts.soak_evidence import (
    COMMIT_FILENAME,
    MANIFEST_FILENAME,
    publish_run,
    read_published_run,
)
from scripts.soak_manifest import SoakManifest
from scripts.soak_samples import SoakSample, validate_sample_series
from tests.benchmarks.test_soak_manifest import COUNTS, RUN_ID, _manifest_data, _samples


def _input() -> tuple[SoakManifest, tuple[SoakSample, ...]]:
    samples = _samples()
    body = b"\n".join(sample.model_dump_json(exclude_none=True).encode() for sample in samples)
    body += b"\n"
    statistics = validate_sample_series(
        samples,
        run_id=RUN_ID,
        scenario_id="long_session",
        expected_measured=COUNTS,
    )
    manifest = SoakManifest.model_validate(_manifest_data(sha256(body).hexdigest(), statistics))
    return manifest, samples


def test_run_publication_round_trip_and_existing_run_rejection(tmp_path) -> None:
    manifest, samples = _input()
    evidence_root = tmp_path / "evidence"

    run_directory, digest = publish_run(evidence_root, manifest, samples)
    loaded, actual_digest = read_published_run(run_directory)

    assert loaded == manifest
    assert actual_digest == digest
    assert {path.name for path in run_directory.iterdir()} == {
        "samples.jsonl",
        MANIFEST_FILENAME,
        COMMIT_FILENAME,
    }
    with pytest.raises(KernelError) as error:
        publish_run(evidence_root, manifest, samples)
    assert error.value.code == "soak_run_exists"
    assert read_published_run(run_directory)[1] == digest


@pytest.mark.parametrize("point", ["after_samples", "after_manifest", "before_commit"])
def test_interruption_before_commit_cannot_be_read_as_published(tmp_path, point) -> None:
    manifest, samples = _input()

    def interrupt(current: str) -> None:
        if current == point:
            raise RuntimeError("预期的发布中断")

    with pytest.raises(RuntimeError, match="预期的发布中断"):
        publish_run(tmp_path / "evidence", manifest, samples, fault=interrupt)
    with pytest.raises(KernelError) as error:
        read_published_run(tmp_path / "evidence" / manifest.run_id)
    assert error.value.code == "soak_run_invalid"


def test_published_run_rejects_sample_manifest_marker_and_extra_file_tampering(tmp_path) -> None:
    manifest, samples = _input()
    run_directory, _ = publish_run(tmp_path / "evidence", manifest, samples)
    original = {path.name: path.read_bytes() for path in run_directory.iterdir()}
    cases = (
        ("samples.jsonl", original["samples.jsonl"].replace(b'"value":100', b'"value":101')),
        (MANIFEST_FILENAME, original[MANIFEST_FILENAME].replace(b'"seed":11', b'"seed":12')),
        (COMMIT_FILENAME, original[COMMIT_FILENAME].replace(b'"manifest_sha256"', b'"digest"')),
    )
    for name, body in cases:
        (run_directory / name).write_bytes(body)
        with pytest.raises(KernelError) as error:
            read_published_run(run_directory)
        assert error.value.code == "soak_run_invalid"
        (run_directory / name).write_bytes(original[name])
    (run_directory / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(KernelError, match="Soak Run提交证据无效"):
        read_published_run(run_directory)


def test_publish_rejects_symlinked_or_nonprivate_root(tmp_path) -> None:
    manifest, samples = _input()
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("当前平台不允许创建目录符号链接")
    with pytest.raises(KernelError) as error:
        publish_run(link, manifest, samples)
    assert error.value.code == "soak_evidence_root_invalid"
    if os.name == "posix":
        public = tmp_path / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with pytest.raises(KernelError) as error:
            publish_run(public, manifest, samples)
        assert error.value.code == "soak_evidence_root_invalid"
