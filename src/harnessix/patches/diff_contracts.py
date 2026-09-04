"""有界精确编辑展示；不是统一补丁、审批凭证或已提交效果。"""

import hashlib
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from harnessix.patches.batch_contracts import MAX_BATCH_FILES
from harnessix.patches.contracts import (
    MAX_EDIT_BYTES,
    MAX_EDITS,
    MAX_PATCH_BYTES,
    patch_path,
    text_bytes,
)
from harnessix.tools.contracts import ReadContract, Revision

MAX_DIFF_BYTES = 1024 * 1024
MAX_DIFF_TEXT_BYTES = 4096
MAX_DIFF_EDITS = MAX_BATCH_FILES * MAX_EDITS


class PatchDiffOptions(ReadContract):
    max_output_bytes: int = Field(default=64 * 1024, ge=256, le=MAX_DIFF_BYTES)
    preview_bytes: int = Field(default=1024, ge=0, le=MAX_DIFF_TEXT_BYTES)


class DiffText(ReadContract):
    text: str = Field(max_length=MAX_DIFF_TEXT_BYTES)
    total_bytes: int = Field(ge=0, le=MAX_EDIT_BYTES)
    sha256: Revision
    truncated: bool

    @model_validator(mode="after")
    def valid_preview(self) -> Self:
        body = text_bytes(self.text)
        if len(body) > min(MAX_DIFF_TEXT_BYTES, self.total_bytes):
            raise ValueError("Diff 文本预览大小不一致")
        if self.truncated != (len(body) < self.total_bytes):
            raise ValueError("Diff 文本截断标记不一致")
        if not self.truncated and hashlib.sha256(body).hexdigest() != self.sha256:
            raise ValueError("完整 Diff 文本摘要不一致")
        return self


class PatchEditDiff(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    patch_fingerprint: Revision
    edit_index: int = Field(ge=0, lt=MAX_EDITS)
    before_start: int = Field(ge=0, le=MAX_PATCH_BYTES)
    after_start: int = Field(ge=0, le=MAX_PATCH_BYTES)
    before: DiffText
    after: DiffText

    _path = field_validator("path")(patch_path)

    @model_validator(mode="after")
    def bounded_ranges(self) -> Self:
        if (
            self.before.total_bytes == 0
            or self.before_start + self.before.total_bytes > MAX_PATCH_BYTES
            or self.after_start + self.after.total_bytes > MAX_PATCH_BYTES
        ):
            raise ValueError("Diff 字节区间无效")
        return self


class PatchBatchDiff(ReadContract):
    version: Literal["patch-batch-diff/v1"] = "patch-batch-diff/v1"
    batch_fingerprint: Revision
    total_files: int = Field(ge=1, le=MAX_BATCH_FILES)
    total_edits: int = Field(ge=1, le=MAX_DIFF_EDITS)
    edits: tuple[PatchEditDiff, ...] = Field(max_length=MAX_DIFF_EDITS)
    truncated: bool

    @model_validator(mode="after")
    def valid_prefix(self) -> Self:
        paths = {edit.path for edit in self.edits}
        if not self.total_files <= self.total_edits <= self.total_files * MAX_EDITS:
            raise ValueError("Diff 文件数与编辑总量不一致")
        if len(self.edits) > self.total_edits or len(paths) > self.total_files:
            raise ValueError("Diff 返回项超过整组总量")
        if len({(e.path, e.edit_index) for e in self.edits}) != len(self.edits):
            raise ValueError("Diff 编辑项重复")
        if self.truncated != (
            len(self.edits) < self.total_edits
            or any(e.before.truncated or e.after.truncated for e in self.edits)
        ):
            raise ValueError("Diff 整体截断标记不一致")
        if len(self.edits) == self.total_edits and len(paths) != self.total_files:
            raise ValueError("完整 Diff 的文件数量不一致")
        return self
