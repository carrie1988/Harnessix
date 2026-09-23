from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import SoakCommit, read_published_run
from scripts.soak_manifest import SoakManifestV4
from scripts.soak_sdk_capacity import _child_result, run_sdk_capacity
from scripts.soak_sdk_proof import SDK_PROOF_FILENAME, SoakSdkProof

REVISION = "a" * 40


def test_child_failure_marker_contains_only_stable_code(tmp_path, monkeypatch) -> None:
    from scripts import soak_sdk_child

    async def fail(*_args):
        raise KernelError("soak_rss_unit_unknown", "private child path and secret")

    monkeypatch.setattr(soak_sdk_child, "_serve", fail)
    with pytest.raises(SystemExit) as caught:
        soak_sdk_child._run_with_failure_marker(tmp_path / "db", tmp_path, 2)
    assert caught.value.code == 1
    assert (tmp_path / "child-failure.txt").read_text(encoding="ascii") == (
        "KernelError:soak_rss_unit_unknown\n"
    )
    with pytest.raises(KernelError) as reported:
        _child_result(tmp_path)
    assert reported.value.code == "soak_sdk_child_failed"
    assert "KernelError:soak_rss_unit_unknown" in reported.value.message
    assert "private child path" not in reported.value.message


async def test_real_sdk_stdio_capacity_cancel_late_response_and_close(tmp_path) -> None:
    evidence = tmp_path / "evidence"
    directory, manifest = await run_sdk_capacity(
        evidence,
        code_revision=REVISION,
        measured_rounds=1,
        warmup_count=0,
        roundtrip_count=3,
        pending_limit=2,
        timeout_seconds=10,
    )
    assert manifest.spec_version == "harnessix.soak-manifest/v4"
    assert manifest.status == "unverified"
    assert manifest.provider.request_count == 0
    assert manifest.fault_counts.cancelled == 1
    assert manifest.sample_counts == {"sdk_roundtrip": 3, "rss_peak": 1}
    assert manifest.file_watermarks.db_after_bytes > 0
    assert read_published_run(directory)[0] == manifest
    assert read_attempt(evidence / "attempts" / manifest.run_id)[1].outcome == "committed"
    proof = SoakSdkProof.model_validate_json((directory / SDK_PROOF_FILENAME).read_bytes())
    assert proof.rounds[0].pending_at_capacity == 2
    assert proof.rounds[0].pending_after_cancel == 1
    assert proof.rounds[0].abandoned_after_cancel == 1
    assert proof.rounds[0].pending_after_late == 2
    assert proof.rounds[0].abandoned_after_late == 0
    assert proof.close_state == "closed"
    body = b"".join(path.read_bytes() for path in directory.iterdir())
    assert str(tmp_path).encode() not in body


async def test_formal_sdk_load_contract_uses_full_negotiated_capacity(
    tmp_path, monkeypatch
) -> None:
    # CI验证相同真实路径；只有干净Revision运行才能生成可冻结的正式外部证据。
    monkeypatch.setattr("scripts.soak_sdk_capacity.check_release_revision", lambda _: None)
    directory, manifest = await run_sdk_capacity(
        tmp_path / "evidence",
        code_revision=REVISION,
        measured_rounds=3,
        warmup_count=1,
        roundtrip_count=20,
        pending_limit=64,
        timeout_seconds=20,
    )
    assert manifest.status == "baseline"
    assert manifest.fault_counts.cancelled == 4
    assert manifest.load.pending_limit == 64
    assert read_published_run(directory)[0] == manifest
    proof = SoakSdkProof.model_validate_json((directory / SDK_PROOF_FILENAME).read_bytes())
    assert [item.phase for item in proof.rounds] == [
        "warmup",
        "measure",
        "measure",
        "measure",
    ]
    assert len(proof.roundtrip_sample_indices) == 20


@pytest.mark.parametrize(
    "rounds,warmup,roundtrips,limit",
    [(0, 0, 1, 2), (21, 0, 1, 2), (1, 2, 1, 2), (3, 0, 20, 64), (3, 1, 19, 64)],
)
async def test_invalid_sdk_load_does_not_begin_attempt(
    tmp_path, rounds: int, warmup: int, roundtrips: int, limit: int
) -> None:
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_sdk_capacity(
            evidence,
            code_revision=REVISION,
            measured_rounds=rounds,
            warmup_count=warmup,
            roundtrip_count=roundtrips,
            pending_limit=limit,
        )
    assert caught.value.code == "soak_load_invalid"
    assert not evidence.exists()


async def test_sdk_marker_timeout_keeps_failed_attempt_without_run(tmp_path, monkeypatch) -> None:
    async def timeout(*_args):
        raise TimeoutError

    monkeypatch.setattr("scripts.soak_sdk_capacity._wait_marker", timeout)
    evidence = tmp_path / "evidence"
    with pytest.raises(KernelError) as caught:
        await run_sdk_capacity(
            evidence,
            code_revision=REVISION,
            measured_rounds=1,
            warmup_count=0,
            roundtrip_count=1,
            pending_limit=2,
            timeout_seconds=10,
        )
    assert caught.value.code == "soak_sdk_timeout"
    attempts = tuple((evidence / "attempts").iterdir())
    assert len(attempts) == 1
    assert read_attempt(attempts[0])[1].outcome == "failed"
    assert not (evidence / attempts[0].name).exists()


async def test_sdk_proof_mutation_is_rejected_by_independent_reader(tmp_path) -> None:
    directory, _ = await run_sdk_capacity(
        tmp_path / "evidence",
        code_revision=REVISION,
        measured_rounds=1,
        warmup_count=0,
        roundtrip_count=1,
        pending_limit=2,
        timeout_seconds=10,
    )
    proof_file = directory / SDK_PROOF_FILENAME
    proof = SoakSdkProof.model_validate_json(proof_file.read_bytes())
    invalid = proof.model_dump(mode="json")
    invalid["rounds"][0]["abandoned_after_cancel"] = 0
    with pytest.raises(ValidationError):
        SoakSdkProof.model_validate(invalid)
    proof_file.write_bytes(proof_file.read_bytes() + b" ")
    with pytest.raises(KernelError) as caught:
        read_published_run(directory)
    assert caught.value.code == "soak_run_invalid"


async def test_sdk_reader_rejects_consistent_hashes_with_inconsistent_rss(tmp_path) -> None:
    directory, manifest = await run_sdk_capacity(
        tmp_path / "evidence",
        code_revision=REVISION,
        measured_rounds=1,
        warmup_count=0,
        roundtrip_count=1,
        pending_limit=2,
        timeout_seconds=10,
    )
    proof_file = directory / SDK_PROOF_FILENAME
    proof = SoakSdkProof.model_validate_json(proof_file.read_bytes())
    tampered = SoakSdkProof.model_validate(
        {**proof.model_dump(mode="json"), "client_rss_bytes": manifest.rss.peak_bytes + 1}
    )
    proof_body = (tampered.model_dump_json() + "\n").encode()
    proof_file.write_bytes(proof_body)
    digest = sha256(proof_body).hexdigest()
    updated = manifest.model_dump(mode="json")
    updated["sdk_proof_sha256"] = digest
    updated["evidence_sha256"][SDK_PROOF_FILENAME] = digest
    updated_manifest = SoakManifestV4.model_validate(updated)
    manifest_body = (updated_manifest.model_dump_json() + "\n").encode()
    (directory / "manifest.json").write_bytes(manifest_body)
    marker = SoakCommit(
        spec_version="harnessix.soak-commit/v1",
        manifest_sha256=sha256(manifest_body).hexdigest(),
    )
    (directory / "COMMITTED.json").write_bytes((marker.model_dump_json() + "\n").encode())
    with pytest.raises(KernelError) as caught:
        read_published_run(directory)
    assert caught.value.code == "soak_run_invalid"


async def test_sdk_runner_cancel_closes_child_and_keeps_failed_attempt(
    tmp_path, monkeypatch
) -> None:
    import scripts.soak_sdk_capacity as sdk_runner

    original_transport = sdk_runner.SubprocessAgentTransport
    transports = []

    def capture_transport(*args, **kwargs):
        transport = original_transport(*args, **kwargs)
        transports.append(transport)
        return transport

    async def cancel_at_marker(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(sdk_runner, "SubprocessAgentTransport", capture_transport)
    monkeypatch.setattr(sdk_runner, "_wait_marker", cancel_at_marker)
    evidence = tmp_path / "evidence"
    with pytest.raises(asyncio.CancelledError):
        await run_sdk_capacity(
            evidence,
            code_revision=REVISION,
            measured_rounds=1,
            warmup_count=0,
            roundtrip_count=1,
            pending_limit=2,
            timeout_seconds=10,
        )
    assert transports[0].snapshot().state == "closed"
    assert transports[0]._child.process.returncode is not None
    attempts = tuple((evidence / "attempts").iterdir())
    assert read_attempt(attempts[0])[1].outcome == "failed"
