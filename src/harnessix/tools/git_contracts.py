"""模型可见Git只读工具的严格、有界契约。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.tools.contracts import ReadContract, Revision

MAX_GIT_STATUS_ENTRIES = 200
MAX_GIT_DIFF_TEXT_BYTES = 48 * 1024


class GitStatusInput(ReadContract):
    limit: int = Field(default=100, ge=1, le=MAX_GIT_STATUS_ENTRIES)


class GitStatusEntry(ReadContract):
    path: str = Field(min_length=1, max_length=4096)
    original_path: str | None = Field(default=None, min_length=1, max_length=4096)
    kind: Literal["ordinary", "renamed", "unmerged", "untracked"]
    index_status: str = Field(min_length=1, max_length=1)
    worktree_status: str = Field(min_length=1, max_length=1)
    submodule: str | None = Field(default=None, min_length=4, max_length=4)

    @model_validator(mode="after")
    def consistent_kind(self) -> Self:
        if (self.kind == "renamed") != (self.original_path is not None):
            raise ValueError("Git重命名记录必须携带唯一原路径")
        return self


class GitStatusOutput(ReadContract):
    branch: str | None = Field(default=None, max_length=4096)
    head_oid: str | None = Field(default=None, max_length=64)
    upstream: str | None = Field(default=None, max_length=4096)
    ahead: int | None = Field(default=None, ge=0)
    behind: int | None = Field(default=None, ge=0)
    entries: tuple[GitStatusEntry, ...] = Field(max_length=MAX_GIT_STATUS_ENTRIES)
    total_entries: int = Field(ge=0)
    truncated: bool
    revision: Revision

    @model_validator(mode="after")
    def consistent_page(self) -> Self:
        if self.total_entries < len(self.entries) or self.truncated != (
            self.total_entries > len(self.entries)
        ):
            raise ValueError("Git状态数量与截断标记不一致")
        if (self.ahead is None) != (self.behind is None):
            raise ValueError("Git上下游差异必须成对出现")
        return self


class GitDiffInput(ReadContract):
    target: Literal["worktree", "staged"] = "worktree"
    context_lines: int = Field(default=3, ge=0, le=20)


class GitDiffOutput(ReadContract):
    target: Literal["worktree", "staged"]
    text: str
    utf8_bytes: int = Field(ge=0, le=MAX_GIT_DIFF_TEXT_BYTES)
    observed_bytes: int = Field(ge=0)
    observed_sha256: Revision
    truncated: bool

    @model_validator(mode="after")
    def consistent_prefix(self) -> Self:
        size = len(self.text.encode("utf-8"))
        if (
            size != self.utf8_bytes
            or self.observed_bytes < size
            or self.truncated != (self.observed_bytes > size)
        ):
            raise ValueError("Git差异文本、观察字节数与截断标记不一致")
        return self
