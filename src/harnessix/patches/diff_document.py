"""只生成完整计划的展示或已归因历史效果，不查询当前目标文件。"""

from dataclasses import dataclass, field
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalRecord
from harnessix.patches.batch_bridge_contracts import (
    ManagedPatchBatchCallPlan,
    ManagedPatchBatchOutput,
)
from harnessix.patches.batch_contracts import PreparedPatchBatch
from harnessix.patches.batch_run_contracts import BatchExecutionResult
from harnessix.patches.batches import validate_patch_batch
from harnessix.patches.diff import _patch_edits
from harnessix.patches.diff_document_contracts import (
    MAX_RECORD_BYTES,
    BatchDiffDocument,
    BatchDiffDocumentOptions,
    BatchDiffEdit,
    BatchDiffFile,
    BatchDiffSummary,
    record_bytes,
)
from harnessix.patches.planner import _checkpoint
from harnessix.tools.workspace import ReadOperation, Workspace


@dataclass(frozen=True, slots=True)
class PreparedBatchDiffDocument:
    """未发布的宿主载荷，不是 ArtifactRef 或 Session 授权；不在 repr 暴露正文。"""

    plan: ManagedPatchBatchCallPlan = field(repr=False)
    approval: ApprovalRecord | None = field(repr=False)
    execution: BatchExecutionResult | None = field(repr=False)
    document: BatchDiffDocument = field(repr=False)


def batch_diff_document(
    workspace: Workspace,
    batch: PreparedPatchBatch,
    operation: ReadOperation,
    *,
    output: ManagedPatchBatchOutput | None = None,
    options: BatchDiffDocumentOptions | None = None,
) -> BatchDiffDocument:
    """纯展示边界；完整调用/批准/运行归属还需由宿主桥接核对。"""
    options = options or BatchDiffDocumentOptions()
    options = BatchDiffDocumentOptions.model_validate_json(options.model_dump_json())
    validate_patch_batch(workspace, batch, operation)
    phase: Literal["not_started", "finished"] | None = None
    if output is not None:
        output = ManagedPatchBatchOutput.model_validate_json(output.model_dump_json())
        if (
            output.phase == "started"
            or len(output.files) != len(batch.patches)
            or any(
                (f.path, f.before_sha256, f.after_sha256)
                != (m.path, m.before_sha256, m.after_sha256)
                for f, m in zip(output.files, batch.manifest.files, strict=True)
            )
        ):
            raise KernelError("patch_diff_source_mismatch", "差异报告缺少匹配的已结算效果")
        phase = "finished" if output.phase == "finished" else "not_started"
    files = tuple(
        BatchDiffFile(
            index=index,
            path=patch.manifest.path,
            patch_fingerprint=patch.manifest.fingerprint,
            before_sha256=patch.manifest.before_sha256,
            after_sha256=patch.manifest.after_sha256,
            before_bytes=patch.manifest.before_bytes,
            after_bytes=patch.manifest.after_bytes,
            total_edits=patch.manifest.edit_count,
            state=output.files[index].state if output else None,
            effect=output.files[index].effect if output else None,
        )
        for index, patch in enumerate(batch.patches)
    )
    selected = tuple(
        patch
        for patch, file in zip(batch.patches, files, strict=True)
        if output is None or file.effect == "applied"
    )
    eligible = sum(p.manifest.edit_count for p in selected)
    summary = BatchDiffSummary(
        view="effect" if output else "plan",
        batch_fingerprint=batch.manifest.fingerprint,
        phase=phase,
        stop_reason=output.stop_reason if output else None,
        effect=output.effect if output else None,
        total_files=len(files),
        eligible_files=len(selected),
        total_edits=sum(f.total_edits for f in files),
        eligible_edits=eligible,
        returned_edits=0,
        complete=eligible == 0,
    )
    size = sum(len(record_bytes(f)) for f in files)
    if size + len(record_bytes(summary)) > options.max_output_bytes:
        raise KernelError("patch_diff_budget_too_small", "预算不足以保留全部成员说明")
    edits: list[BatchDiffEdit] = []
    truncated = False
    for edit in _patch_edits(selected, operation, options.preview_bytes):
        row = BatchDiffEdit.model_validate_json(edit.model_dump_json())
        encoded = record_bytes(row)
        if len(encoded) > MAX_RECORD_BYTES:
            raise KernelError("patch_diff_record_too_large", "单条差异记录超过分页上限")
        more_truncated = truncated or edit.before.truncated or edit.after.truncated
        candidate = summary.model_copy(
            update={
                "returned_edits": len(edits) + 1,
                "complete": len(edits) + 1 == eligible and not more_truncated,
            }
        )
        if size + len(encoded) + len(record_bytes(candidate)) > options.max_output_bytes:
            break
        edits.append(row)
        size += len(encoded)
        summary, truncated = candidate, more_truncated
    _checkpoint(operation)
    return BatchDiffDocument(summary=summary, files=files, edits=tuple(edits))
