"""Trusted Container Action的有界、二进制安全输出文档。"""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import re
from collections.abc import Callable
from typing import Annotated, Literal, NamedTuple, Self, cast

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_PAGE_BYTES
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.public_output import (
    MAX_PROCESS_PREVIEW_STREAM_BYTES,
    MAX_PROCESS_TAIL_PREVIEW_STREAM_BYTES,
)
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


_CURRENT_PREVIEW_VERSION = "trusted-process-output/v4"
LegacyPreviewVersion = Literal["trusted-process-output/v2", "trusted-process-output/v3"]


def trusted_process_public_output(
    document: TrustedProcessOutputDocument,
    *,
    include_passed: bool = False,
    include_preview: bool = False,
    legacy_preview: LegacyPreviewVersion | None = None,
) -> dict[str, JsonValue]:
    """从同一验真文档派生公开合同；默认精确保留历史v1及Eval摘要。

    include_preview派生当前v4预览；legacy_preview只用于按原版本重建已审计的v2前缀或v3尾部预览。
    """

    if include_passed and (include_preview or legacy_preview):
        raise ValueError("Eval v1合同不包含Process诊断预览")
    if include_preview and legacy_preview:
        raise ValueError("Process公开输出只能选择一个预览版本")
    public = document.summary.public_output()
    if include_preview:
        public["version"] = _CURRENT_PREVIEW_VERSION
        public["diagnostic_preview"] = {
            name: _stream_display_tail(document, name) for name in ("stdout", "stderr")
        }
    if legacy_preview:
        build = _LEGACY_PREVIEWS[legacy_preview]
        public["version"] = legacy_preview
        public["diagnostic_preview"] = {
            name: build(document, name) for name in ("stdout", "stderr")
        }
    if include_passed:
        summary = document.summary
        public["passed"] = (
            summary.state == "exited"
            and summary.stop_reason == "exited"
            and summary.returncode == 0
        )
    return public


def recorded_process_public_output(
    document: TrustedProcessOutputDocument,
    current: dict[str, JsonValue],
    expected_sha256: str,
) -> dict[str, JsonValue] | None:
    """按原审计Hash选择同一文档的公开形状：当前合同，或当前预览版之前的各预览版与v1纯摘要。

    只重建已审计事实，不给旧终态追加新预览；Eval等无预览形状没有历史候选。
    """

    candidates = [current]
    versions = (_CURRENT_PREVIEW_VERSION, *LEGACY_PREVIEW_VERSIONS)
    version = current.get("version")
    if isinstance(version, str) and version in versions:
        candidates.extend(
            trusted_process_public_output(document, legacy_preview=older)
            for older in LEGACY_PREVIEW_VERSIONS[versions.index(version) :]
        )
        candidates.append(trusted_process_public_output(document))
    return next(
        (item for item in candidates if canonical_digest(item) == expected_sha256),
        None,
    )


# 终端转义序列（ECMA-48/35）：CSI如颜色与光标移动；OSC与DCS、SOS、PM、APC控制串以BEL或ST结束，
# 控制串内容不含C0控制字符，因此不会越过终止符吞掉后续输出；其余为ESC后接可选中间字节与一个
# 终止字节，如ESC 7、ESC ( B。未闭合或畸形的ESC不匹配，仍按控制字符使整流不可显示，不展示残片。
_TERMINAL_ESCAPE_SEQUENCE = re.compile(
    rb"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    rb"|\x1b[\]PX^_][^\x00-\x1f\x7f]*(?:\x07|\x1b\\)"
    rb"|\x1b(?![\[\]PX^_])[\x20-\x2f]*[\x30-\x7e]"
)


class _DisplayStream(NamedTuple):
    # 去除转义序列后的可显示UTF-8正文，及被去除序列在原归档流中按位置升序的[起, 止)字节区间。
    text: bytes
    escapes: tuple[tuple[int, int], ...]


def _archived_stream(document: TrustedProcessOutputDocument, name: str) -> bytes:
    return b"".join(chunk.data() for chunk in document.chunks if chunk.stream == name)


def _is_displayable(text: str) -> bool:
    return all(char.isprintable() or char in "\t\r\n" for char in text)


def _visible_stream(document: TrustedProcessOutputDocument, name: str) -> bytes | None:
    """v2/v3规则：整个归档前缀须是可显示UTF-8；不将后半段含乱码/控制符的流伪装成普通文本。"""
    data = _archived_stream(document, name)
    try:
        decoded = data.decode("utf-8")
    except UnicodeError:
        return None
    return data if _is_displayable(decoded) else None


def _display_stream(document: TrustedProcessOutputDocument, name: str) -> _DisplayStream | None:
    """v4规则：原流须是合法UTF-8，去除完整终端转义序列后整个正文仍须可显示。

    转义序列全由ASCII组成，在合法UTF-8中只落在字符边界上，去除后不会拼出原流没有的字符。
    """
    data = _archived_stream(document, name)
    try:
        data.decode("utf-8")
    except UnicodeError:
        return None
    escapes = tuple(match.span() for match in _TERMINAL_ESCAPE_SEQUENCE.finditer(data))
    text = _TERMINAL_ESCAPE_SEQUENCE.sub(b"", data)
    return _DisplayStream(text, escapes) if _is_displayable(text.decode("utf-8")) else None


def _stream_preview(document: TrustedProcessOutputDocument, name: str) -> dict[str, JsonValue]:
    data = _visible_stream(document, name)
    text = (
        None
        if data is None
        else codecs.getincrementaldecoder("utf-8")().decode(
            data[:MAX_PROCESS_PREVIEW_STREAM_BYTES], final=False
        )
    )
    size = len(text.encode("utf-8")) if text is not None else 0
    summary = document.summary.stdout if name == "stdout" else document.summary.stderr
    return {"text": text, "size_bytes": size, "truncated": size < summary.observed_bytes}


def _tail_start(data: bytes) -> int:
    """最后至多8 KiB的起点，后移到完整UTF-8字符。"""
    start = max(0, len(data) - MAX_PROCESS_TAIL_PREVIEW_STREAM_BYTES)
    while start < len(data) and data[start] & 0xC0 == 0x80:
        start += 1
    return start


def _stream_tail(document: TrustedProcessOutputDocument, name: str) -> dict[str, JsonValue]:
    """v3：取归档流最后至多8 KiB；offset为起点在流中的字节偏移。"""
    summary = document.summary.stdout if name == "stdout" else document.summary.stderr
    data = _visible_stream(document, name)
    if data is None:
        return {"text": None, "offset": 0, "size_bytes": 0, "truncated": True}
    start = _tail_start(data)
    size = len(data) - start
    return {
        "text": data[start:].decode("utf-8"),
        "offset": start,
        "size_bytes": size,
        "truncated": size < summary.observed_bytes,
    }


def _stream_display_tail(document: TrustedProcessOutputDocument, name: str) -> dict[str, JsonValue]:
    """v4：去除转义序列后取正文最后至多8 KiB，8 KiB预算只花在可读正文上。

    预览覆盖原流[offset, 归档末尾)：offset紧接最后一个未展示的正文字节，区间内被去除的转义
    序列计入removed_escape_bytes，所以offset+size_bytes+removed_escape_bytes等于归档字节数；
    truncated仍表示预览之外另有输出，只有颜色码被去除的完整流不算截断。
    """
    summary = document.summary.stdout if name == "stdout" else document.summary.stderr
    stream = _display_stream(document, name)
    if stream is None:
        return {
            "text": None,
            "offset": 0,
            "size_bytes": 0,
            "removed_escape_bytes": 0,
            "truncated": True,
        }
    start = _tail_start(stream.text)
    offset = _raw_offset(stream.escapes, start)
    removed = sum(end - begin for begin, end in stream.escapes if begin >= offset)
    size = len(stream.text) - start
    return {
        "text": stream.text[start:].decode("utf-8"),
        "offset": offset,
        "size_bytes": size,
        "removed_escape_bytes": removed,
        "truncated": size + removed < summary.observed_bytes,
    }


def _raw_offset(escapes: tuple[tuple[int, int], ...], text_start: int) -> int:
    """正文第text_start字节之前那个正文字节在原流中的结束位置；text_start为0时是流首。"""
    if text_start == 0:
        return 0
    position = text_start - 1
    for begin, end in escapes:
        if begin > position:
            break
        position += end - begin
    return position + 1


# 历史预览版本由新到旧；重建已审计终态时只尝试原版本及更早的形状，不给旧事实追加新预览。
_LEGACY_PREVIEWS: dict[
    LegacyPreviewVersion, Callable[[TrustedProcessOutputDocument, str], dict[str, JsonValue]]
] = {
    "trusted-process-output/v3": _stream_tail,
    "trusted-process-output/v2": _stream_preview,
}
LEGACY_PREVIEW_VERSIONS: tuple[LegacyPreviewVersion, ...] = tuple(_LEGACY_PREVIEWS)


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
