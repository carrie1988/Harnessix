"""直接使用精确编辑区间生成有界预览，不重新运行通用文本匹配算法。"""

import hashlib

from harnessix.patches.batch_contracts import PreparedPatchBatch
from harnessix.patches.batches import validate_patch_batch
from harnessix.patches.diff_contracts import (
    DiffText,
    PatchBatchDiff,
    PatchDiffOptions,
    PatchEditDiff,
)
from harnessix.patches.planner import _checkpoint, _edit_ranges
from harnessix.tools.workspace import ReadOperation, Workspace


def _preview(body: bytes, limit: int) -> DiffText:
    # 正文已通过计划 UTF-8 校验；ignore 只丢弃边界上未完整的最后一个码点。
    text = body[:limit].decode("utf-8", errors="ignore")
    return DiffText(
        text=text,
        total_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        truncated=len(text.encode()) < len(body),
    )


def patch_batch_diff(
    workspace: Workspace,
    batch: PreparedPatchBatch,
    operation: ReadOperation,
    options: PatchDiffOptions | None = None,
) -> PatchBatchDiff:
    """只展示计划；内部校验不读取当前文件，不声称效果已发生。"""
    options = options or PatchDiffOptions()
    options = PatchDiffOptions.model_validate_json(options.model_dump_json())
    validate_patch_batch(workspace, batch, operation)
    report = PatchBatchDiff(
        batch_fingerprint=batch.manifest.fingerprint,
        total_files=len(batch.patches),
        total_edits=sum(len(p.proposal.edits) for p in batch.patches),
        edits=(),
        truncated=True,
    )
    for patch in batch.patches:
        shift = 0
        for index, (start, stop, replacement) in enumerate(
            _edit_ranges(patch.before, patch.proposal, operation)
        ):
            _checkpoint(operation)
            edit = PatchEditDiff(
                path=patch.manifest.path,
                patch_fingerprint=patch.manifest.fingerprint,
                edit_index=index,
                before_start=start,
                after_start=start + shift,
                before=_preview(patch.before[start:stop], options.preview_bytes),
                after=_preview(replacement, options.preview_bytes),
            )
            edits = (*report.edits, edit)
            candidate = report.model_copy(
                update={
                    "edits": edits,
                    "truncated": len(edits) < report.total_edits
                    or any(e.before.truncated or e.after.truncated for e in edits),
                }
            )
            if len(candidate.model_dump_json().encode()) > options.max_output_bytes:
                _checkpoint(operation)
                return report
            report = candidate
            shift += len(replacement) - (stop - start)
    _checkpoint(operation)
    return report
