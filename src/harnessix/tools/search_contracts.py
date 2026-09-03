from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from harnessix.tools.contracts import ReadContract, Revision

MAX_SEARCH_ENTRIES = 10000
MAX_SEARCH_NAMES_BYTES = 2 * 1024 * 1024
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SEARCH_RECORD_BYTES = 24 * 1024
MAX_MATCH_BYTES = 384
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

    @field_validator("include")
    @classmethod
    def check_pattern(cls, value: str) -> str:
        return validate_pattern(value)

    @field_validator("query")
    @classmethod
    def check_query(cls, value: str) -> str:
        return _bounded_text(value)


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


class GrepMatch(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    line: int = Field(ge=1)
    text: str
    text_truncated: bool
    revision: Revision

    @field_validator("text")
    @classmethod
    def validate_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_MATCH_BYTES:
            raise ValueError("命中片段超过 UTF-8 上限")
        return value


class GrepOutput(SearchOutput):
    matches: tuple[GrepMatch, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        positions = [(hit.path, hit.line) for hit in self.matches]
        if positions != sorted(set(positions)) or (self.truncated and not positions):
            raise ValueError("命中行必须有序、无重复且截断时非空")
        return self
