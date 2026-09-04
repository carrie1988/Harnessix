"""只读整组准备/复核；复用单文件准备器，不接触写账本。"""

import json

from harnessix.agent.errors import KernelError
from harnessix.patches.batch_contracts import (
    MAX_BATCH_IMAGE_BYTES,
    PatchBatchManifest,
    PatchBatchProposal,
    PreparedPatchBatch,
)
from harnessix.patches.contracts import PreparedPatch
from harnessix.patches.planner import (
    _checkpoint,
    prepare_patch,
    validate_prepared,
    verify_prepared,
)
from harnessix.tools.workspace import ReadOperation, Workspace, digest


def _manifest(
    workspace: Workspace, proposal: PatchBatchProposal, patches: tuple[PreparedPatch, ...]
) -> PatchBatchManifest:
    data = {
        "version": "patch-batch-plan/v1",
        "workspace_scope": workspace.scope,
        "proposal_sha256": digest(proposal.model_dump(mode="json")),
        "files": [patch.manifest.model_dump(mode="json") for patch in patches],
    }
    return PatchBatchManifest.model_validate_json(json.dumps({**data, "fingerprint": digest(data)}))


def prepare_patch_batch(
    workspace: Workspace, proposal: PatchBatchProposal, operation: ReadOperation
) -> PreparedPatchBatch:
    _checkpoint(operation)
    proposal = PatchBatchProposal.model_validate_json(proposal.model_dump_json())
    patches = []
    size = 0
    for member in proposal.files:
        patch = prepare_patch(workspace, member, operation)
        size += len(patch.before) + len(patch.after)
        if size > MAX_BATCH_IMAGE_BYTES:
            raise KernelError("patch_batch_limit_exceeded", "整组完整镜像总量超限")
        patches.append(patch)
    members = tuple(patches)
    batch = PreparedPatchBatch(_manifest(workspace, proposal, members), proposal, members)
    # 较晚文件准备期间，较早文件可能变化；仍不宣称同时刻快照或提交 CAS。
    verify_patch_batch(workspace, batch, operation)
    return batch


def validate_patch_batch(
    workspace: Workspace, batch: PreparedPatchBatch, operation: ReadOperation
) -> None:
    _checkpoint(operation)
    try:
        proposal = PatchBatchProposal.model_validate_json(batch.proposal.model_dump_json())
        manifest = PatchBatchManifest.model_validate_json(batch.manifest.model_dump_json())
        if (
            type(batch.patches) is not tuple
            or len(batch.patches) != len(proposal.files)
            or any(type(p) is not PreparedPatch for p in batch.patches)
            or tuple(p.proposal for p in batch.patches) != proposal.files
        ):
            raise ValueError("成员与提案不匹配")
        for patch in batch.patches:
            validate_prepared(workspace, patch, operation)
        if manifest != _manifest(workspace, proposal, batch.patches):
            raise ValueError("整组 manifest 与成员不匹配")
    except ValueError:
        raise KernelError("patch_batch_corrupt", "整组提案、载荷或 manifest 不一致") from None


def verify_patch_batch(
    workspace: Workspace, batch: PreparedPatchBatch, operation: ReadOperation
) -> None:
    validate_patch_batch(workspace, batch, operation)
    for patch in batch.patches:
        verify_prepared(workspace, patch, operation)
    _checkpoint(operation)
