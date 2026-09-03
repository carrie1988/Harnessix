from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from harnessix.tools.contracts import ReadContract, ReadToolError, Revision
from harnessix.tools.workspace import _parts, digest

MAX_PATCH_BYTES = 1024 * 1024
MAX_EDIT_BYTES = 128 * 1024
MAX_PROPOSAL_BYTES = 256 * 1024
MAX_EDITS = 32


def text_bytes(value: str) -> bytes:
    try:
        result = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("编辑文本必须是合法 UTF-8") from None
    if any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in value):
        raise ValueError("编辑文本含不支持的控制字符")
    return result


def patch_path(value: str) -> str:
    try:
        if not _parts(value):
            raise ValueError("Patch 必须定位具体文件")
    except ReadToolError:
        raise ValueError("Patch 路径必须是受限相对路径") from None
    return value


class ExactEdit(ReadContract):
    old_text: str = Field(min_length=1, max_length=MAX_EDIT_BYTES)
    new_text: str = Field(max_length=MAX_EDIT_BYTES)

    @model_validator(mode="after")
    def valid_edit(self) -> Self:
        if self.old_text == self.new_text:
            raise ValueError("编辑必须改变内容")
        if len(text_bytes(self.old_text)) + len(text_bytes(self.new_text)) > MAX_EDIT_BYTES:
            raise ValueError("单编辑 UTF-8 大小超限")
        return self


class PatchProposal(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    expected_revision: Revision
    edits: tuple[ExactEdit, ...] = Field(min_length=1, max_length=MAX_EDITS)

    _path = field_validator("path")(patch_path)

    @model_validator(mode="after")
    def bounded_proposal(self) -> Self:
        if (
            sum(len(e.old_text.encode()) + len(e.new_text.encode()) for e in self.edits)
            > MAX_PROPOSAL_BYTES
        ):
            raise ValueError("全部编辑 UTF-8 总量超限")
        return self


class PatchManifest(ReadContract):
    version: Literal["patch-plan/v1"] = "patch-plan/v1"
    path: str = Field(min_length=1, max_length=1024)
    workspace_scope: Revision
    source_revision: Revision
    source_mode: int = Field(ge=0, le=0o777)
    proposal_sha256: Revision
    before_sha256: Revision
    after_sha256: Revision
    before_bytes: int = Field(ge=0, le=MAX_PATCH_BYTES)
    after_bytes: int = Field(ge=0, le=MAX_PATCH_BYTES)
    edit_count: int = Field(ge=1, le=MAX_EDITS)
    fingerprint: Revision

    _path = field_validator("path")(patch_path)

    @model_validator(mode="after")
    def valid_fingerprint(self) -> Self:
        if self.fingerprint != digest(self.model_dump(mode="json", exclude={"fingerprint"})):
            raise ValueError("Patch manifest 指纹不一致")
        if self.before_sha256 == self.after_sha256:
            raise ValueError("Patch 不包含内容变化")
        return self


@dataclass(frozen=True, slots=True)
class PreparedPatch:
    """宿主私有内容，不是审批凭证或已提交效果；使用前须复核。"""

    manifest: PatchManifest
    proposal: PatchProposal = field(repr=False)
    before: bytes = field(repr=False)
    after: bytes = field(repr=False)
