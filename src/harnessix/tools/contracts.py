from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from harnessix.domain.models import ContractModel

MAX_TEXT_BYTES = 24 * 1024
MAX_LINE_BYTES = 4096
MAX_SCAN_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 10000
MAX_RESULT_BYTES = 60000
READ_TIMEOUT_SECONDS = 5
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReadContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ListFilesInput(ReadContract):
    path: str = Field(default=".", min_length=1, max_length=1024)
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=MAX_DIRECTORY_ENTRIES)
    expected_revision: Revision | None = None

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.offset and self.expected_revision is None:
            raise ValueError("后续页必须携带 revision")
        return self


class ReadFileInput(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(default=1, ge=1, le=MAX_SCAN_BYTES)
    max_lines: int = Field(default=200, ge=1, le=2000)
    expected_revision: Revision | None = None

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.start_line > 1 and self.expected_revision is None:
            raise ValueError("后续页必须携带 revision")
        return self


class DirectoryEntry(ReadContract):
    name: str = Field(min_length=1, max_length=255)
    kind: Literal["file", "directory", "symlink", "special"]


class ListFilesOutput(ReadContract):
    path: str
    entries: tuple[DirectoryEntry, ...] = Field(max_length=200)
    revision: Revision
    truncated: bool
    next_offset: int | None = Field(ge=1)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.truncated != (self.next_offset is not None):
            raise ValueError("分页状态与下一位置不一致")
        names = [entry.name for entry in self.entries]
        if names != sorted(set(names)) or (self.truncated and not names):
            raise ValueError("目录页必须有序、无重复且截断时非空")
        return self


class ReadFileOutput(ReadContract):
    path: str
    text: str
    start_line: int = Field(ge=1)
    end_line: int | None = Field(ge=1)
    utf8_bytes: int = Field(ge=0, le=MAX_TEXT_BYTES)
    revision: Revision
    truncated: bool
    truncation_reason: Literal["line_limit", "byte_limit"] | None
    next_line: int | None = Field(ge=2)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        lines = self.text.count("\n") + int(bool(self.text) and not self.text.endswith("\n"))
        if self.truncated and not lines:
            raise ValueError("截断页必须返回可继续的非空片段")
        if self.utf8_bytes != len(self.text.encode("utf-8")) or self.end_line != (
            self.start_line + lines - 1 if lines else None
        ):
            raise ValueError("读取范围与内容不一致")
        if self.truncated != (self.truncation_reason is not None) or self.next_line != (
            self.end_line + 1 if self.truncated and self.end_line is not None else None
        ):
            raise ValueError("截断状态与下一行不一致")
        return self


ReadErrorCode = Literal[
    "path_denied",
    "not_found",
    "wrong_file_type",
    "invalid_utf8",
    "binary_file",
    "limit_exceeded",
    "workspace_changed",
    "page_changed",
    "offset_out_of_range",
    "io_failed",
    "timeout",
]


class ReadToolError(Exception):
    def __init__(self, code: ReadErrorCode) -> None:
        super().__init__(code)
        self.code = code
