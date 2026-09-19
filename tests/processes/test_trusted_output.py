from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from harnessix.processes.supervision_contracts import (
    ProcessLease,
    ProcessOutputObservation,
)
from harnessix.processes.trusted_output import (
    MAX_TRUSTED_PROCESS_ARCHIVE_BYTES,
    TrustedProcessOutputDocument,
    build_trusted_process_output,
    parse_trusted_process_output,
    trusted_process_public_output,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def observation(data: bytes, *, observed: bytes | None = None, eof: bool = True):
    complete = data if observed is None else observed
    return ProcessOutputObservation(
        observed_bytes=len(complete),
        persisted_bytes=len(data),
        sha256=hashlib.sha256(complete).hexdigest(),
        persisted_sha256=hashlib.sha256(data).hexdigest(),
        truncated=len(data) < len(complete),
        eof=eof,
    )


def lease(stdout: bytes, stderr: bytes, *, returncode: int = 0) -> ProcessLease:
    process_id = uuid4()
    return ProcessLease(
        process_id=process_id,
        plan_id=process_id,
        plan_fingerprint="1" * 64,
        process_spec_digest="2" * 64,
        capability_digest="3" * 64,
        launch_binding_digest="4" * 64,
        lifecycle="foreground",
        state="exited",
        sequence=4,
        owner_token="5" * 64,
        owner_identity="6" * 64,
        pid=1234,
        deadline=NOW + timedelta(minutes=1),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        returncode=returncode,
        stop_reason="exited",
        stdout=observation(stdout),
        stderr=observation(stderr),
    )


def test_trusted_process_output_is_binary_safe_canonical_and_publicly_bounded() -> None:
    stdout = b"hello\x00\xff\n"
    stderr = "错误\n".encode()
    document = build_trusted_process_output("unit-tests", lease(stdout, stderr), stdout, stderr)
    body = document.to_jsonl()

    assert parse_trusted_process_output(body) == document
    assert document.summary.complete
    assert document.summary.public_output()["profile"] == "unit-tests"
    assert trusted_process_public_output(document, include_passed=True)["passed"] is True
    assert b"hello\x00" not in body
    assert len(body) < 1024 * 1024


def test_trusted_process_output_fairly_archives_large_dual_streams() -> None:
    stdout = b"a" * (400 * 1024)
    stderr = b"b" * (400 * 1024)
    document = build_trusted_process_output("unit-tests", lease(stdout, stderr), stdout, stderr)

    assert (
        document.summary.stdout.archived_bytes + document.summary.stderr.archived_bytes
        == MAX_TRUSTED_PROCESS_ARCHIVE_BYTES
    )
    assert document.summary.stdout.archived_bytes == MAX_TRUSTED_PROCESS_ARCHIVE_BYTES // 2
    assert document.summary.stderr.archived_bytes == MAX_TRUSTED_PROCESS_ARCHIVE_BYTES // 2
    assert document.summary.stdout.archive_truncated
    assert document.summary.stderr.archive_truncated
    assert not document.summary.complete
    assert len(document.to_jsonl()) < 1024 * 1024


def test_trusted_process_test_result_is_derived_from_terminal_facts() -> None:
    failed = build_trusted_process_output(
        "unit-tests",
        lease(b"failed\n", b"", returncode=1),
        b"failed\n",
        b"",
    )

    assert "passed" not in trusted_process_public_output(failed)
    assert trusted_process_public_output(failed, include_passed=True)["passed"] is False


def test_trusted_process_output_rejects_lease_body_and_jsonl_tampering() -> None:
    current = lease(b"abcdef", b"")
    with pytest.raises(ValueError, match="持久输出"):
        build_trusted_process_output("unit-tests", current, b"abcdeg", b"")

    document = build_trusted_process_output("unit-tests", current, b"abcdef", b"")
    changed = document.model_dump()
    changed["chunks"][0]["offset"] = 1
    with pytest.raises(ValueError):
        TrustedProcessOutputDocument.model_validate(changed)
    with pytest.raises(ValueError, match="正文损坏"):
        parse_trusted_process_output(document.to_jsonl().replace(b'"offset":0', b'"offset":1'))
