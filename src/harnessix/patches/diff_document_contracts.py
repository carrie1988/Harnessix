"""计划/历史效果的有界 JSONL；报告完整不等于执行成功。"""

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from harnessix.patches.batch_contracts import MAX_BATCH_FILES
from harnessix.patches.batch_run_contracts import (
    BatchEffect,
    MemberEffect,
    StopReason,
    member_effect,
    ordered_progress,
)
from harnessix.patches.contracts import MAX_EDITS, MAX_PATCH_BYTES, patch_path
from harnessix.patches.diff_contracts import MAX_DIFF_EDITS, PatchEditDiff
from harnessix.patches.managed_contracts import PatchState
from harnessix.tools.contracts import ReadContract, Revision

# 与 Artifact 的公开 JSONL 限制一致；不导入有 Session 依赖的发布器。
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 24 * 1024


class BatchDiffDocumentOptions(ReadContract):
    max_output_bytes: int = Field(default=64 * 1024, ge=1024, le=MAX_DOCUMENT_BYTES)
    preview_bytes: int = Field(default=1024, ge=0, le=4096)


class BatchDiffSummary(ReadContract):
    kind: Literal["summary"] = "summary"
    version: Literal["patch-batch-diff-document/v1"] = "patch-batch-diff-document/v1"
    view: Literal["plan", "effect"]
    batch_fingerprint: Revision
    phase: Literal["not_started", "finished"] | None = None
    stop_reason: StopReason | None = None
    effect: BatchEffect | None = None
    total_files: int = Field(ge=1, le=MAX_BATCH_FILES)
    eligible_files: int = Field(ge=0, le=MAX_BATCH_FILES)
    total_edits: int = Field(ge=1, le=MAX_DIFF_EDITS)
    eligible_edits: int = Field(ge=0, le=MAX_DIFF_EDITS)
    returned_edits: int = Field(ge=0, le=MAX_DIFF_EDITS)
    complete: bool


class BatchDiffFile(ReadContract):
    kind: Literal["file"] = "file"
    index: int = Field(ge=0, lt=MAX_BATCH_FILES)
    path: str = Field(min_length=1, max_length=1024)
    patch_fingerprint: Revision
    before_sha256: Revision
    after_sha256: Revision
    before_bytes: int = Field(ge=0, le=MAX_PATCH_BYTES)
    after_bytes: int = Field(ge=0, le=MAX_PATCH_BYTES)
    total_edits: int = Field(ge=1, le=MAX_EDITS)
    state: PatchState | None = None
    effect: MemberEffect | None = None

    _path = field_validator("path")(patch_path)


class BatchDiffEdit(PatchEditDiff):
    kind: Literal["edit"] = "edit"


BatchDiffRecord = Annotated[
    BatchDiffSummary | BatchDiffFile | BatchDiffEdit, Field(discriminator="kind")
]


def record_bytes(record: ReadContract) -> bytes:
    return record.model_dump_json().encode() + b"\n"


class BatchDiffDocument(ReadContract):
    summary: BatchDiffSummary
    files: tuple[BatchDiffFile, ...] = Field(min_length=1, max_length=MAX_BATCH_FILES)
    edits: tuple[BatchDiffEdit, ...] = Field(max_length=MAX_DIFF_EDITS)

    def to_jsonl(self) -> bytes:
        return b"".join(record_bytes(r) for r in (self.summary, *self.files, *self.edits))

    @model_validator(mode="after")
    def valid_document(self) -> Self:
        summary = self.summary
        if (
            len({f.path for f in self.files}) != len(self.files)
            or tuple(f.index for f in self.files) != tuple(range(len(self.files)))
            or summary.total_files != len(self.files)
            or summary.total_edits != sum(f.total_edits for f in self.files)
        ):
            raise ValueError("整组报告文件顺序或总量不一致")
        if summary.view == "plan":
            if (
                summary.effect is not None
                or summary.phase is not None
                or summary.stop_reason is not None
                or any(f.state is not None or f.effect is not None for f in self.files)
            ):
                raise ValueError("计划展示不能伪造执行状态")
        else:
            states = []
            for file in self.files:
                if file.state is None or file.effect != member_effect(file.state):
                    raise ValueError("成员历史效果与状态不一致")
                states.append(file.state)
            ordered_progress(tuple(states))
            effects = {f.effect for f in self.files}
            aggregate = (
                "unknown"
                if "unknown" in effects
                else "applied"
                if effects == {"applied"}
                else "partial"
                if "applied" in effects
                else "not_applied"
            )
            if (
                summary.phase is None
                or summary.effect != aggregate
                or (summary.phase == "finished") != (summary.stop_reason is not None)
                or (summary.phase == "not_started" and any(s != "pending" for s in states))
                or (summary.stop_reason == "completed" and summary.effect != "applied")
            ):
                raise ValueError("历史效果的阶段、原因或聚合值不一致")
        selected = tuple(f for f in self.files if summary.view == "plan" or f.effect == "applied")
        keys = tuple((f.path, i) for f in selected for i in range(f.total_edits))
        if (
            summary.eligible_files != len(selected)
            or summary.eligible_edits != len(keys)
            or summary.returned_edits != len(self.edits)
            or len(self.edits) > len(keys)
            or tuple((e.path, e.edit_index) for e in self.edits) != keys[: len(self.edits)]
            or summary.complete
            != (
                len(self.edits) == len(keys)
                and all(not e.before.truncated and not e.after.truncated for e in self.edits)
            )
        ):
            raise ValueError("报告编辑必须是所选成员的有序前缀，不能隐藏截断")
        files = {f.path: f for f in self.files}
        shifts: dict[str, int] = {}
        ends: dict[str, int] = {}
        for edit in self.edits:
            file = files[edit.path]
            if (
                edit.patch_fingerprint != file.patch_fingerprint
                or edit.before_start < ends.get(edit.path, 0)
                or edit.after_start != edit.before_start + shifts.get(edit.path, 0)
                or edit.before_start + edit.before.total_bytes > file.before_bytes
                or edit.after_start + edit.after.total_bytes > file.after_bytes
            ):
                raise ValueError("编辑身份或 UTF-8 字节区间与文件不一致")
            ends[edit.path] = edit.before_start + edit.before.total_bytes
            shifts[edit.path] = (
                shifts.get(edit.path, 0) + edit.after.total_bytes - edit.before.total_bytes
            )
        lines = tuple(record_bytes(r) for r in (summary, *self.files, *self.edits))
        if (
            any(len(line) > MAX_RECORD_BYTES for line in lines)
            or sum(map(len, lines)) > MAX_DOCUMENT_BYTES
        ):
            raise ValueError("JSONL 记录或完整报告超过字节上限")
        return self
