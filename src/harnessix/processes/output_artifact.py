"""ProcessResult已捕获双流的有界、二进制安全 JSONL 文档。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_PAGE_BYTES
from harnessix.processes.contracts import MAX_CAPTURE_BYTES, ProcessResult, ProcessStream
from harnessix.tools.contracts import ReadContract, Revision

PROCESS_OUTPUT_CHUNK_BYTES = 12 * 1024
MAX_PROCESS_OUTPUT_CHUNKS = 2 * (
    (MAX_CAPTURE_BYTES + PROCESS_OUTPUT_CHUNK_BYTES - 1) // PROCESS_OUTPUT_CHUNK_BYTES
)


class ProcessOutputStreamSummary(ReadContract):
    stream: Literal["stdout", "stderr"]
    captured_bytes: int = Field(ge=0, le=MAX_CAPTURE_BYTES)
    captured_sha256: Revision
    observed_bytes: int = Field(ge=0)
    observed_sha256: Revision
    truncated: bool
    eof: bool

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.observed_bytes < self.captured_bytes or self.truncated != (
            self.observed_bytes > self.captured_bytes
        ):
            raise ValueError("进程流归档摘要与捕获字节数不一致")
        return self


class ProcessOutputSummary(ReadContract):
    kind: Literal["summary"] = "summary"
    version: Literal["process-output/v1"] = "process-output/v1"
    stdout: ProcessOutputStreamSummary
    stderr: ProcessOutputStreamSummary
    complete: bool

    @model_validator(mode="after")
    def truthful_completeness(self) -> Self:
        expected = all(stream.eof and not stream.truncated for stream in (self.stdout, self.stderr))
        if self.complete != expected:
            raise ValueError("进程输出完整性与双流证据不一致")
        return self


class ProcessOutputChunk(ReadContract):
    kind: Literal["chunk"] = "chunk"
    stream: Literal["stdout", "stderr"]
    offset: int = Field(ge=0, le=MAX_CAPTURE_BYTES)
    size_bytes: int = Field(ge=1, le=PROCESS_OUTPUT_CHUNK_BYTES)
    data_base64: str = Field(max_length=4 * ((PROCESS_OUTPUT_CHUNK_BYTES + 2) // 3), repr=False)

    def data(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)

    @model_validator(mode="after")
    def canonical_data(self) -> Self:
        data = self.data()
        if (
            len(data) != self.size_bytes
            or base64.b64encode(data).decode("ascii") != self.data_base64
        ):
            raise ValueError("进程输出分片不满足规范Base64和字节长度")
        return self


ProcessOutputRecord = Annotated[
    ProcessOutputSummary | ProcessOutputChunk, Field(discriminator="kind")
]


def _record_bytes(record: ReadContract) -> bytes:
    return record.model_dump_json().encode() + b"\n"


def _reject_constant(_: str) -> None:
    raise ValueError("JSON不允许非有限数字")


class ProcessOutputDocument(ReadContract):
    summary: ProcessOutputSummary
    chunks: tuple[ProcessOutputChunk, ...] = Field(max_length=MAX_PROCESS_OUTPUT_CHUNKS)

    def to_jsonl(self) -> bytes:
        return b"".join(_record_bytes(record) for record in (self.summary, *self.chunks))

    @model_validator(mode="after")
    def complete_captured_prefix(self) -> Self:
        grouped: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        previous_stream = "stdout"
        for chunk in self.chunks:
            if previous_stream == "stderr" and chunk.stream == "stdout":
                raise ValueError("进程输出分片顺序不稳定")
            previous_stream = chunk.stream
            data = grouped[chunk.stream]
            if chunk.offset != len(data):
                raise ValueError("进程输出分片偏移不连续")
            data.extend(chunk.data())
        for stream in (self.summary.stdout, self.summary.stderr):
            captured = bytes(grouped[stream.stream])
            if (
                len(captured) != stream.captured_bytes
                or hashlib.sha256(captured).hexdigest() != stream.captured_sha256
                or (
                    not stream.truncated
                    and hashlib.sha256(captured).hexdigest() != stream.observed_sha256
                )
            ):
                raise ValueError("进程输出分片与流摘要不一致")
        lines = tuple(_record_bytes(record) for record in (self.summary, *self.chunks))
        if any(len(line) > MAX_PAGE_BYTES for line in lines) or sum(map(len, lines)) > (
            MAX_ARTIFACT_BYTES
        ):
            raise ValueError("进程输出文档超过Artifact字节边界")
        return self


def _summary(
    name: Literal["stdout", "stderr"], stream: ProcessStream
) -> ProcessOutputStreamSummary:
    data = stream.data()
    return ProcessOutputStreamSummary(
        stream=name,
        captured_bytes=stream.captured_bytes,
        captured_sha256=hashlib.sha256(data).hexdigest(),
        observed_bytes=stream.observed_bytes,
        observed_sha256=stream.observed_sha256,
        truncated=stream.truncated,
        eof=stream.eof,
    )


def _chunks(
    name: Literal["stdout", "stderr"], stream: ProcessStream
) -> tuple[ProcessOutputChunk, ...]:
    data = stream.data()
    return tuple(
        ProcessOutputChunk(
            stream=name,
            offset=offset,
            size_bytes=len(chunk),
            data_base64=base64.b64encode(chunk).decode("ascii"),
        )
        for offset in range(0, len(data), PROCESS_OUTPUT_CHUNK_BYTES)
        if (chunk := data[offset : offset + PROCESS_OUTPUT_CHUNK_BYTES])
    )


def process_output_document(result: ProcessResult) -> ProcessOutputDocument | None:
    """全量保存Action已捕获前缀；超过Artifact上限时不二次截断。"""
    checked = ProcessResult.model_validate_json(result.model_dump_json())
    stdout, stderr = _summary("stdout", checked.stdout), _summary("stderr", checked.stderr)
    document = ProcessOutputDocument.model_construct(
        summary=ProcessOutputSummary(
            stdout=stdout,
            stderr=stderr,
            complete=all(stream.eof and not stream.truncated for stream in (stdout, stderr)),
        ),
        chunks=(*_chunks("stdout", checked.stdout), *_chunks("stderr", checked.stderr)),
    )
    try:
        return ProcessOutputDocument.model_validate_json(document.model_dump_json())
    except ValueError as error:
        if "Artifact字节边界" in str(error):
            return None
        raise


def parse_process_output_document(body: bytes) -> ProcessOutputDocument:
    """读取时重建强类型文档，并拒绝非规范JSONL或重复摘要。"""
    try:
        raw = body.decode("utf-8")
        lines = raw.splitlines()
        adapter: TypeAdapter[ProcessOutputRecord] = TypeAdapter(ProcessOutputRecord)
        parsed = [
            adapter.validate_python(json.loads(line, parse_constant=_reject_constant))
            for line in lines
        ]
        if not parsed or not isinstance(parsed[0], ProcessOutputSummary):
            raise ValueError("缺少摘要")
        if any(isinstance(record, ProcessOutputSummary) for record in parsed[1:]):
            raise ValueError("摘要重复")
        document = ProcessOutputDocument(
            summary=parsed[0],
            chunks=tuple(record for record in parsed[1:] if isinstance(record, ProcessOutputChunk)),
        )
        if document.to_jsonl() != body:
            raise ValueError("正文不是规范JSONL")
        return document
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError, RecursionError):
        raise ValueError("Process Artifact正文损坏") from None
