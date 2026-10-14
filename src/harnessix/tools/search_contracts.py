"""内置Coding Tool：定义搜索输入、分页游标、统计和结果合同。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from harnessix.artifacts.contracts import ArtifactRef
from harnessix.tools.contracts import ReadContract, Revision

MAX_SEARCH_ENTRIES = 10000
MAX_SEARCH_NAMES_BYTES = 2 * 1024 * 1024
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SEARCH_RECORD_BYTES = 24 * 1024
MAX_MATCH_BYTES = 384
MAX_GREP_CONTEXT_LINES = 3
IGNORED_DIRECTORIES = (
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
)


def _bounded_text(value: str) -> str:
    if len(value.encode("utf-8")) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("查询或模式超过 UTF-8 上限或含控制字符")
    return value


def validate_pattern(value: str) -> str:
    _bounded_text(value)
    parts = value.split("/")
    if (
        len(parts) > 32
        or any(p in {"", ".", ".."} for p in parts)
        or any(c in value for c in "\\{}")
    ):
        raise ValueError("模式必须是受支持的相对路径通配")
    return value


class SearchInput(ReadContract):
    path: str = Field(default=".", min_length=1, max_length=1024)
    max_results: int = Field(default=100, ge=1, le=200)
    include_ignored: bool = False


class GlobInput(SearchInput):
    pattern: str = Field(default="**/*", min_length=1, max_length=256)

    @field_validator("pattern")
    @classmethod
    def check_pattern(cls, value: str) -> str:
        return validate_pattern(value)


class GrepInput(SearchInput):
    query: str = Field(min_length=1, max_length=256)
    include: str = Field(default="**/*", min_length=1, max_length=256)
    context_lines: int = Field(
        default=0,
        ge=0,
        le=MAX_GREP_CONTEXT_LINES,
        description="命中行前后各返回少量有界片段；不替代read_file的完整文件摘要",
    )

    @field_validator("include")
    @classmethod
    def check_pattern(cls, value: str) -> str:
        return validate_pattern(value)

    @field_validator("query")
    @classmethod
    def check_query(cls, value: str) -> str:
        return _bounded_text(value)


class GrepSnapshotInput(GrepInput):
    max_results: int = Field(default=10, ge=1, le=200)
    context_lines: int = Field(
        default=2,
        ge=0,
        le=MAX_GREP_CONTEXT_LINES,
        description="命中行前后各返回少量有界片段；不替代read_file的完整文件摘要",
    )


class RegexGrepInput(GrepSnapshotInput):
    query: str = Field(
        min_length=1,
        max_length=256,
        description="match_mode=regex时是原样RE2表达式；literal时是字面量，不隐式修正或转义",
    )
    match_mode: Literal["regex", "literal"] = Field(
        default="regex", description="逐行查找首个命中；RE2不支持反向引用或前后查找断言"
    )


class SearchStats(ReadContract):
    entries_scanned: int = Field(ge=0, le=MAX_SEARCH_ENTRIES)
    files_read: int = Field(ge=0, le=MAX_SEARCH_ENTRIES)
    bytes_read: int = Field(ge=0, le=MAX_SEARCH_TOTAL_BYTES)
    ignored_entries: int = Field(ge=0)
    unreadable_entries: int = Field(ge=0)
    oversized_files: int = Field(ge=0)
    invalid_utf8_files: int = Field(ge=0)
    binary_files: int = Field(ge=0)
    long_lines: int = Field(ge=0)

    @property
    def has_gaps(self) -> bool:
        return any(
            (
                self.unreadable_entries,
                self.oversized_files,
                self.invalid_utf8_files,
                self.binary_files,
                self.long_lines,
            )
        )


class SearchOutput(ReadContract):
    path: str
    stats: SearchStats
    scan_complete: bool
    truncated: bool
    truncation_reason: Literal["result_limit", "output_limit"] | None

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.truncated != (self.truncation_reason is not None) or self.scan_complete != (
            not self.truncated and not self.stats.has_gaps
        ):
            raise ValueError("搜索完整性与截断/跳过事实不一致")
        return self


class GlobOutput(SearchOutput):
    paths: tuple[str, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if list(self.paths) != sorted(set(self.paths)) or (self.truncated and not self.paths):
            raise ValueError("定位结果必须有序、无重复且截断时非空")
        return self


class GrepContextLine(ReadContract):
    text: str
    text_truncated: bool

    @field_validator("text")
    @classmethod
    def validate_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_MATCH_BYTES or "\n" in value:
            raise ValueError("上下文必须是单行有界UTF-8片段")
        return value


class GrepContext(ReadContract):
    start_line: int = Field(ge=1)
    lines: tuple[GrepContextLine, ...] = Field(
        min_length=1, max_length=2 * MAX_GREP_CONTEXT_LINES + 1
    )


class GrepMatch(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    line: int = Field(ge=1)
    text: str
    text_truncated: bool
    revision: Revision
    context: GrepContext | None = None

    @field_validator("text")
    @classmethod
    def validate_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_MATCH_BYTES:
            raise ValueError("命中片段超过 UTF-8 上限")
        return value

    @model_validator(mode="after")
    def context_contains_same_match(self) -> Self:
        if self.context is None:
            return self
        offset = self.line - self.context.start_line
        if (
            not 0 <= offset < len(self.context.lines)
            or offset > MAX_GREP_CONTEXT_LINES
            or len(self.context.lines) - offset - 1 > MAX_GREP_CONTEXT_LINES
        ):
            raise ValueError("上下文必须包含命中行且两侧均未超过上限")
        center = self.context.lines[offset]
        if (center.text, center.text_truncated) != (self.text, self.text_truncated):
            raise ValueError("上下文中的命中片段与原命中不一致")
        return self


class GrepSnapshotMatch(GrepMatch):
    content_sha256: Revision
    file_bytes: int = Field(ge=0, le=MAX_SEARCH_FILE_BYTES)


class GrepOutput(SearchOutput):
    matches: tuple[GrepMatch, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        positions = [(hit.path, hit.line) for hit in self.matches]
        if positions != sorted(set(positions)) or (self.truncated and not positions):
            raise ValueError("命中行必须有序、无重复且截断时非空")
        return self


class GrepSnapshotOutput(GrepOutput):
    spec_version: Literal["harnessix.grep-snapshot/v1"] = "harnessix.grep-snapshot/v1"
    matches: tuple[GrepSnapshotMatch, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def same_file_observation_is_consistent(self) -> Self:
        observations: dict[str, tuple[str, str, int]] = {}
        for hit in self.matches:
            snapshot = (hit.revision, hit.content_sha256, hit.file_bytes)
            if observations.setdefault(hit.path, snapshot) != snapshot:
                raise ValueError("同一搜索结果中同文件的观察身份、摘要和字节数必须一致")
        return self


class ArchivedGlobOutput(ReadContract):
    preview: GlobOutput
    artifact: ArtifactRef


class ArchivedGrepOutput(ReadContract):
    preview: GrepOutput
    artifact: ArtifactRef


class ArchivedGrepSnapshotOutput(ReadContract):
    preview: GrepSnapshotOutput
    artifact: ArtifactRef
