"""Git私有raw结果的成功和解析边界；合成观察不代表原生Windows验收。"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import FrozenInstanceError, fields

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.processes.contracts import ProcessResult, ProcessStream
from harnessix.processes.git_observation import GitBaselineReadResult
from harnessix.processes.owner_receipt import RawProcessOutputObservation
from harnessix.tools.contracts import ReadToolError


def raw(body: bytes, *, eof: bool = True) -> RawProcessOutputObservation:
    return RawProcessOutputObservation(
        observed_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), eof=eof
    )


def result(body: bytes = b"safe") -> ProcessResult:
    def stream(data: bytes) -> ProcessStream:
        return ProcessStream(
            data_base64=base64.b64encode(data).decode(),
            captured_bytes=len(data),
            observed_bytes=len(data),
            observed_sha256=hashlib.sha256(data).hexdigest(),
            truncated=False,
            eof=True,
        )

    return ProcessResult(
        pid=123,
        returncode=0,
        stop_reason="exited",
        termination="none",
        stdout=stream(body),
        stderr=stream(b""),
        elapsed_seconds=0.0,
    )


@pytest.mark.parametrize("original", [b"tiny", b"a-very-long-synthetic-protected-value"])
def test_private_result_keeps_safe_stream_separate_from_raw(original):
    safe = result(b"[REDACTED]")
    observed = GitBaselineReadResult(safe, raw(original), raw(b""))
    assert observed.result is safe
    assert safe.stdout.observed_bytes == len(b"[REDACTED]")
    assert observed.raw_stdout.observed_bytes == len(original)
    assert observed.raw_stdout.sha256 == hashlib.sha256(original).hexdigest()
    assert [field.name for field in fields(observed)] == ["result", "raw_stdout", "raw_stderr"]
    assert repr(observed) == "GitBaselineReadResult()"
    assert not hasattr(observed, "model_dump")
    with pytest.raises(FrozenInstanceError):
        observed.raw_stdout = raw(b"replacement")


@pytest.mark.parametrize("original", [b"safe", b"other-length", b"SAFE"])
def test_metadata_requires_complete_equal_length_and_digest(original):
    observed = GitBaselineReadResult(result(), raw(original), raw(b""))
    if original == b"safe":
        assert observed.full_stdout() == b"safe"
    else:
        with pytest.raises(KernelError) as denied:
            observed.full_stdout()
        assert denied.value.code == "git_baseline_metadata_changed"


def test_metadata_rejects_truncated_prefix_even_when_prefix_matches_raw():
    safe = result()
    safe = safe.model_copy(update={"stdout": safe.stdout.model_copy(update={"truncated": True})})
    observed = GitBaselineReadResult(safe, raw(b"safe"), raw(b""))
    with pytest.raises(ReadToolError) as denied:
        observed.full_stdout()
    assert denied.value.code == "limit_exceeded"


@pytest.mark.parametrize("source", ["safe", "raw"])
@pytest.mark.parametrize("name", ["stdout", "stderr"])
def test_either_stream_without_eof_cannot_supply_raw_proof(source, name):
    safe, stdout, stderr = result(), raw(b"safe"), raw(b"")
    if source == "safe":
        safe = safe.model_copy(update={name: getattr(safe, name).model_copy(update={"eof": False})})
    elif name == "stdout":
        stdout = raw(b"safe", eof=False)
    else:
        stderr = raw(b"", eof=False)
    with pytest.raises(ReadToolError) as denied:
        GitBaselineReadResult(safe, stdout, stderr)
    assert denied.value.code == "io_failed"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"stop_reason": "cancelled"}, "cancelled"),
        ({"stop_reason": "timeout"}, "timeout"),
        ({"stop_reason": "output_limit"}, "io_failed"),
        ({"stop_reason": "closed"}, "io_failed"),
        ({"stop_reason": "io_error"}, "io_failed"),
        ({"stop_reason": "cleanup_failed", "termination": "failed"}, "io_failed"),
        ({"returncode": 1}, "io_failed"),
        ({"termination": "term"}, "io_failed"),
    ],
)
def test_dual_eof_does_not_override_failed_process_outcome(changes, code):
    safe = result().model_copy(update=changes)
    if code == "cancelled":
        with pytest.raises(TurnCancelled):
            GitBaselineReadResult(safe, raw(b"safe"), raw(b""))
    else:
        with pytest.raises(ReadToolError) as denied:
            GitBaselineReadResult(safe, raw(b"safe"), raw(b""))
        assert denied.value.code == code


def test_posix_projection_uses_full_capture_digest_without_larger_prefix():
    body = b"a" * (1024 * 1024 + 17)
    prefix = body[: 1024 * 1024]
    safe = result().model_copy(
        update={
            "stdout": ProcessStream(
                data_base64=base64.b64encode(prefix).decode(),
                captured_bytes=len(prefix),
                observed_bytes=len(body),
                observed_sha256=hashlib.sha256(body).hexdigest(),
                truncated=True,
                eof=True,
            )
        }
    )
    observed = GitBaselineReadResult.from_posix_capture(safe)
    assert observed.result.stdout.data() == prefix
    assert observed.raw_stdout == raw(body)
    assert observed.raw_stderr == raw(b"")
