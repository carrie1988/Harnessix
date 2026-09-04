"""多文件只读计划契约；不代表已批准或已执行。"""

from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.patches.contracts import PatchManifest, PatchProposal, PreparedPatch
from harnessix.tools.contracts import ReadContract, Revision
from harnessix.tools.workspace import digest

MAX_BATCH_FILES = 16
MAX_BATCH_PROPOSAL_BYTES = 512 * 1024
MAX_BATCH_IMAGE_BYTES = 8 * 1024 * 1024


class PatchBatchProposal(ReadContract):
    files: tuple[PatchProposal, ...] = Field(min_length=1, max_length=MAX_BATCH_FILES)

    @model_validator(mode="after")
    def bounded_files(self) -> Self:
        if len({p.path for p in self.files}) != len(self.files):
            raise ValueError("整组提案不能重复定位同一路径")
        if (
            sum(
                len(edit.old_text.encode()) + len(edit.new_text.encode())
                for proposal in self.files
                for edit in proposal.edits
            )
            > MAX_BATCH_PROPOSAL_BYTES
        ):
            raise ValueError("整组提案 UTF-8 总量超限")
        return self


class PatchBatchManifest(ReadContract):
    version: Literal["patch-batch-plan/v1"] = "patch-batch-plan/v1"
    workspace_scope: Revision
    proposal_sha256: Revision
    files: tuple[PatchManifest, ...] = Field(min_length=1, max_length=MAX_BATCH_FILES)
    fingerprint: Revision

    @model_validator(mode="after")
    def bound_files(self) -> Self:
        if len({m.path for m in self.files}) != len(self.files):
            raise ValueError("整组计划不能重复定位同一路径")
        if any(m.workspace_scope != self.workspace_scope for m in self.files):
            raise ValueError("整组计划必须属于同一工作区")
        if sum(m.before_bytes + m.after_bytes for m in self.files) > MAX_BATCH_IMAGE_BYTES:
            raise ValueError("整组完整镜像总量超限")
        if self.fingerprint != digest(self.model_dump(mode="json", exclude={"fingerprint"})):
            raise ValueError("整组计划指纹不一致")
        return self


@dataclass(frozen=True, slots=True)
class PreparedPatchBatch:
    manifest: PatchBatchManifest
    proposal: PatchBatchProposal = field(repr=False)
    patches: tuple[PreparedPatch, ...] = field(repr=False)
