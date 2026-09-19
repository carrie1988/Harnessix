from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.processes.contracts import ProcessResult, ProcessStream
from harnessix.processes.output_artifact import (
    ProcessOutputDocument,
    ProcessOutputRecord,
    parse_process_output_document,
    process_output_document,
)


def _stream(data: bytes, *, observed: bytes | None = None, eof: bool = True) -> ProcessStream:
    full = data if observed is None else observed
    return ProcessStream(
        data_base64=base64.b64encode(data).decode("ascii"),
        captured_bytes=len(data),
        observed_bytes=len(full),
        observed_sha256=hashlib.sha256(full).hexdigest(),
        truncated=len(full) > len(data),
        eof=eof,
    )


def _result(stdout: bytes, stderr: bytes = b"") -> ProcessResult:
    return ProcessResult(
        pid=123,
        returncode=0,
        stop_reason="exited",
        termination="none",
        stdout=_stream(stdout),
        stderr=_stream(stderr),
        elapsed_seconds=0.1,
    )


def test_historical_process_output_is_binary_safe_and_canonical() -> None:
    process = _result(b"a\x00\xff" * 5000, "中文\n".encode())
    document = process_output_document(process)
    assert document is not None and document.summary.complete
    body = document.to_jsonl()

    restored = parse_process_output_document(body)

    assert restored == document
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    for chunk in restored.chunks:
        streams[chunk.stream].extend(chunk.data())
    assert bytes(streams["stdout"]) == process.stdout.data()
    assert bytes(streams["stderr"]) == process.stderr.data()
    assert max(map(len, body.splitlines(keepends=True))) <= 24 * 1024
    assert b'"pid"' not in body and b"action_id" not in body and b"arguments" not in body
    assert TypeAdapter(ProcessOutputRecord).validate_json(body.splitlines()[0])


def test_historical_process_output_never_hides_second_truncation() -> None:
    assert process_output_document(_result(b"x" * 400_000, b"y" * 400_000)) is None
    incomplete = ProcessResult(
        **_result(b"abc").model_dump(exclude={"stdout"}),
        stdout=_stream(b"abc", observed=b"abcdef", eof=False),
    )
    document = process_output_document(incomplete)
    assert document is not None and not document.summary.complete


def test_historical_process_output_rejects_tampered_chunks() -> None:
    document = process_output_document(_result(b"abcdef"))
    assert document is not None
    data = document.model_dump(mode="json")
    data["chunks"][0]["offset"] = 1
    with pytest.raises(ValidationError):
        ProcessOutputDocument.model_validate(data)
    with pytest.raises(ValueError, match="损坏"):
        parse_process_output_document(document.to_jsonl().replace(b'"offset":0', b'"offset":1'))


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        ("process-output-record", TypeAdapter(ProcessOutputRecord).json_schema()),
        ("process-output-document", ProcessOutputDocument.model_json_schema()),
    ],
)
def test_historical_process_output_schema_remains_frozen(
    name: str, schema: dict[str, object]
) -> None:
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert json.loads(path.read_text()) == schema
