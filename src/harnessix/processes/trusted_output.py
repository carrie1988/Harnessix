"""Trusted Container Action的有界、二进制安全输出文档。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Annotated, Literal, Self, cast

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_PAGE_BYTES
from harnessix.processes.supervision_contracts import ProcessLease, ProcessOutputObservation
from harnessix.tools.contracts import ReadContract, Revision

TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES = 12 * 1024
MAX_TRUSTED_PROCESS_ARCHIVE_BYTES = 512 * 1024
MAX_TRUSTED_PROCESS_OUTPUT_CHUNKS = 2 * (
    (MAX_TRUSTED_PROCESS_ARCHIVE_BYTES + TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES - 1)
    // TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES
)


class TrustedProcessStreamSummary(ReadContract):
    """同时记录Owner观察、持久前缀和Artifact归档前缀。"""

    stream: Literal["stdout", "stderr"]
    observed_bytes: int = Field(ge=0)
    observed_sha256: Revision
    persisted_bytes: int = Field(ge=0)
    persisted_sha256: Revision
    archived_bytes: int = Field(ge=0, le=MAX_TRUSTED_PROCESS_ARCHIVE_BYTES)
    archived_sha256: Revision
    truncated: bool
    archive_truncated: bool
    eof: bool

    @model_validator(mode="after")
    def consistent_prefixes(self) -> Self:
        empty = hashlib.sha256(b"").hexdigest()
        if (
            self.archived_bytes > self.persisted_bytes
            or self.persisted_bytes > self.observed_bytes
            or self.truncated != (self.persisted_bytes < self.observed_bytes)
            or self.archive_truncated != (self.archived_bytes < self.persisted_bytes)
            or (self.observed_bytes == 0 and self.observed_sha256 != empty)
            or (self.persisted_bytes == 0 and self.persisted_sha256 != empty)
            or (self.archived_bytes == 0 and self.archived_sha256 != empty)
            or (
                self.persisted_bytes == self.observed_bytes
                and self.persisted_sha256 != self.observed_sha256
            )
            or (
                self.archived_bytes == self.persisted_bytes
                and self.archived_sha256 != self.persisted_sha256
            )
        ):
            raise ValueError("Trusted Process输出前缀摘要不一致")
        return self


class TrustedProcessOutputSummary(ReadContract):
    """模型可见摘要与Artifact首记录共享的唯一事实。"""

    kind: Literal["summary"] = "summary"
    version: Literal["trusted-process-output/v1"] = "trusted-process-output/v1"
    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    process_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    state: Literal["exited", "failed", "unknown"]
    returncode: int | None = None
    stop_reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    stdout: TrustedProcessStreamSummary
    stderr: TrustedProcessStreamSummary
    complete: bool

    @model_validator(mode="after")
    def truthful_terminal_summary(self) -> Self:
        streams = (self.stdout, self.stderr)
        expected_complete = all(
            item.eof and not item.truncated and not item.archive_truncated for item in streams
        )
        if (
            self.stdout.stream != "stdout"
            or self.stderr.stream != "stderr"
            or (self.state == "exited") != (self.returncode is not None)
            or self.complete != expected_complete
        ):
            raise ValueError("Trusted Process终态摘要不一致")
        return self

    def public_output(self) -> dict[str, JsonValue]:
        """返回不含归档内部字段、但可独立验证的有界Tool Result摘要。"""

        def stream(value: TrustedProcessStreamSummary) -> dict[str, JsonValue]:
            return {
                "observed_bytes": value.observed_bytes,
                "observed_sha256": value.observed_sha256,
                "persisted_bytes": value.persisted_bytes,
                "truncated": value.truncated,
                "eof": value.eof,
            }

        return {
            "version": self.version,
            "profile": self.profile,
            "process_id": self.process_id,
            "state": self.state,
            "returncode": self.returncode,
            "stop_reason": self.stop_reason,
            "stdout": stream(self.stdout),
            "stderr": stream(self.stderr),
            "complete": self.complete,
        }


class TrustedProcessOutputChunk(ReadContract):
    kind: Literal["chunk"] = "chunk"
    stream: Literal["stdout", "stderr"]
    offset: int = Field(ge=0, le=MAX_TRUSTED_PROCESS_ARCHIVE_BYTES)
    size_bytes: int = Field(ge=1, le=TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES)
    data_base64: str = Field(
        max_length=4 * ((TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES + 2) // 3), repr=False
    )

    def data(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)

    @model_validator(mode="after")
    def canonical_data(self) -> Self:
        data = self.data()
        if (
            len(data) != self.size_bytes
            or base64.b64encode(data).decode("ascii") != self.data_base64
        ):
            raise ValueError("Trusted Process输出分片不是规范Base64")
        return self


TrustedProcessOutputRecord = Annotated[
    TrustedProcessOutputSummary | TrustedProcessOutputChunk,
    Field(discriminator="kind"),
]


def _record_bytes(record: ReadContract) -> bytes:
    return record.model_dump_json().encode("utf-8") + b"\n"


class TrustedProcessOutputDocument(ReadContract):
    summary: TrustedProcessOutputSummary
    chunks: tuple[TrustedProcessOutputChunk, ...] = Field(
        max_length=MAX_TRUSTED_PROCESS_OUTPUT_CHUNKS
    )

    def to_jsonl(self) -> bytes:
        return b"".join(_record_bytes(item) for item in (self.summary, *self.chunks))

    @model_validator(mode="after")
    def complete_archived_prefix(self) -> Self:
        grouped: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        previous = "stdout"
        for chunk in self.chunks:
            if previous == "stderr" and chunk.stream == "stdout":
                raise ValueError("Trusted Process输出分片顺序不稳定")
            previous = chunk.stream
            data = grouped[chunk.stream]
            if chunk.offset != len(data):
                raise ValueError("Trusted Process输出分片偏移不连续")
            data.extend(chunk.data())
        for stream in (self.summary.stdout, self.summary.stderr):
            archived = bytes(grouped[stream.stream])
            if (
                len(archived) != stream.archived_bytes
                or hashlib.sha256(archived).hexdigest() != stream.archived_sha256
            ):
                raise ValueError("Trusted Process归档正文与摘要不一致")
        lines = tuple(_record_bytes(item) for item in (self.summary, *self.chunks))
        if any(len(line) > MAX_PAGE_BYTES for line in lines) or sum(map(len, lines)) > (
            MAX_ARTIFACT_BYTES
        ):
            raise ValueError("Trusted Process输出文档超过Artifact边界")
        return self


def _archive_lengths(stdout_bytes: int, stderr_bytes: int) -> tuple[int, int]:
    half = MAX_TRUSTED_PROCESS_ARCHIVE_BYTES // 2
    stdout = min(stdout_bytes, half)
    stderr = min(stderr_bytes, half)
    remaining = MAX_TRUSTED_PROCESS_ARCHIVE_BYTES - stdout - stderr
    extra_stdout = min(stdout_bytes - stdout, remaining)
    stdout += extra_stdout
    remaining -= extra_stdout
    stderr += min(stderr_bytes - stderr, remaining)
    return stdout, stderr


def _stream_summary(
    name: Literal["stdout", "stderr"],
    observation: ProcessOutputObservation,
    archived: bytes,
) -> TrustedProcessStreamSummary:
    return TrustedProcessStreamSummary(
        stream=name,
        observed_bytes=observation.observed_bytes,
        observed_sha256=observation.sha256,
        persisted_bytes=observation.persisted_bytes,
        persisted_sha256=observation.persisted_sha256,
        archived_bytes=len(archived),
        archived_sha256=hashlib.sha256(archived).hexdigest(),
        truncated=observation.truncated,
        archive_truncated=len(archived) < observation.persisted_bytes,
        eof=observation.eof,
    )


def _chunks(
    name: Literal["stdout", "stderr"], data: bytes
) -> tuple[TrustedProcessOutputChunk, ...]:
    return tuple(
        TrustedProcessOutputChunk(
            stream=name,
            offset=offset,
            size_bytes=len(chunk),
            data_base64=base64.b64encode(chunk).decode("ascii"),
        )
        for offset in range(0, len(data), TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES)
        if (chunk := data[offset : offset + TRUSTED_PROCESS_OUTPUT_CHUNK_BYTES])
    )


def build_trusted_process_output(
    profile: str,
    lease: ProcessLease,
    stdout: bytes,
    stderr: bytes,
) -> TrustedProcessOutputDocument:
    """从终态Lease及其已验证持久前缀构造确定性归档。"""

    checked = ProcessLease.model_validate_json(lease.model_dump_json())
    if checked.state not in {"exited", "failed", "unknown"}:
        raise ValueError("Trusted Process输出只能由终态Lease构造")
    for body, observation in ((stdout, checked.stdout), (stderr, checked.stderr)):
        if (
            type(body) is not bytes
            or len(body) != observation.persisted_bytes
            or hashlib.sha256(body).hexdigest() != observation.persisted_sha256
        ):
            raise ValueError("Trusted Process持久输出与Lease不一致")
    stdout_length, stderr_length = _archive_lengths(len(stdout), len(stderr))
    archived_stdout, archived_stderr = stdout[:stdout_length], stderr[:stderr_length]
    stdout_summary = _stream_summary("stdout", checked.stdout, archived_stdout)
    stderr_summary = _stream_summary("stderr", checked.stderr, archived_stderr)
    summary = TrustedProcessOutputSummary(
        profile=profile,
        process_id=str(checked.process_id),
        state=cast(Literal["exited", "failed", "unknown"], checked.state),
        returncode=checked.returncode,
        stop_reason=cast(str, checked.stop_reason),
        stdout=stdout_summary,
        stderr=stderr_summary,
        complete=all(
            item.eof and not item.truncated and not item.archive_truncated
            for item in (stdout_summary, stderr_summary)
        ),
    )
    return TrustedProcessOutputDocument(
        summary=summary,
        chunks=(*_chunks("stdout", archived_stdout), *_chunks("stderr", archived_stderr)),
    )


def _reject_constant(_: str) -> None:
    raise ValueError("JSON不允许非有限数字")


def parse_trusted_process_output(body: bytes) -> TrustedProcessOutputDocument:
    """严格解析规范JSONL，并拒绝重复摘要、乱码和重新编码差异。"""

    try:
        lines = body.decode("utf-8").splitlines()
        adapter: TypeAdapter[TrustedProcessOutputRecord] = TypeAdapter(TrustedProcessOutputRecord)
        parsed = [
            adapter.validate_python(json.loads(line, parse_constant=_reject_constant))
            for line in lines
        ]
        if not parsed or not isinstance(parsed[0], TrustedProcessOutputSummary):
            raise ValueError("缺少摘要")
        if any(isinstance(item, TrustedProcessOutputSummary) for item in parsed[1:]):
            raise ValueError("摘要重复")
        document = TrustedProcessOutputDocument(
            summary=parsed[0],
            chunks=tuple(
                item for item in parsed[1:] if isinstance(item, TrustedProcessOutputChunk)
            ),
        )
        if document.to_jsonl() != body:
            raise ValueError("正文不是规范JSONL")
        return document
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError, RecursionError):
        raise ValueError("Trusted Process Artifact正文损坏") from None
