"""Artifact manifest与Session权威引用的一致性校验。"""

from __future__ import annotations

from uuid import UUID

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    Thread,
    ToolResultContent,
    Turn,
)
from harnessix.agent.reducer import get_turn
from harnessix.artifacts.action_output_store import validate_action_output_reference
from harnessix.artifacts.action_review_store import validate_action_review_reference
from harnessix.artifacts.contracts import ArtifactRef

_PURPOSES = frozenset(
    {
        "tool_result",
        "batch_plan",
        "batch_effect",
        "process_output",
        "action_review",
        "action_output",
    }
)


def validate_artifact_reference(row: aiosqlite.Row, thread: Thread) -> ArtifactRef:
    """解析manifest并按用途证明其由同一Session项目引用。"""

    try:
        ref = ArtifactRef.model_validate_json(row["manifest_json"])
        if (
            str(ref.artifact_id) != row["artifact_id"]
            or ref.size_bytes != row["size_bytes"]
            or ref.expires_at.isoformat() != row["expires_at"]
            or row["purpose"] not in _PURPOSES
        ):
            raise ValueError("Artifact索引或用途不匹配")
        turn = get_turn(thread, UUID(row["turn_id"]))
        purpose = row["purpose"]
        if purpose == "action_review":
            return validate_action_review_reference(row, turn, ref)
        if purpose == "action_output":
            return validate_action_output_reference(row, thread, turn, ref)
        if purpose == "process_output":
            return _process_reference(row, turn, ref)
        if purpose != "tool_result":
            return _batch_reference(row, turn, ref)
        return _tool_result_reference(row, turn, ref)
    except KernelError as error:
        if error.code == "artifact_unreferenced":
            raise
        raise KernelError("artifact_corrupt", "Artifact manifest 或结果引用不一致") from None
    except (ValueError, StopIteration):
        raise KernelError("artifact_corrupt", "Artifact manifest 或结果引用不一致") from None


def _process_reference(
    row: aiosqlite.Row,
    turn: Turn,
    ref: ArtifactRef,
) -> ArtifactRef:
    results = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
        and item.status == ItemStatus.COMPLETED
        and str(item.content.call_id) == row["call_id"]
        and item.content.process is not None
    ]
    requests = [
        item.content
        for item in turn.items
        if isinstance(item.content, ProcessApprovalRequestContent)
        and item.status == ItemStatus.COMPLETED
        and str(item.content.call_id) == row["call_id"]
    ]
    if len(results) != 1 or len(requests) != 1:
        raise ValueError("Process输出引用数量不匹配")
    result, request = results[0], requests[0]
    if (
        not isinstance(result.output, dict)
        or result.output.get("artifact") != ref.model_dump(mode="json")
        or result.process is None
        or result.action_id != request.plan.action_id
        or result.process.action_id != request.plan.action_id
        or result.process.action_fingerprint != request.plan.action_fingerprint
        or result.process.plan_fingerprint != request.plan.approval_fingerprint
    ):
        raise ValueError("Process输出引用不匹配")
    return ref


def _batch_reference(row: aiosqlite.Row, turn: Turn, ref: ArtifactRef) -> ArtifactRef:
    contents = [
        item.content
        for item in turn.items
        if isinstance(item.content, PatchBatchApprovalRequestContent | ToolResultContent)
        and (
            isinstance(item.content, PatchBatchApprovalRequestContent)
            if row["purpose"] == "batch_plan"
            else isinstance(item.content, ToolResultContent)
            and item.status == ItemStatus.COMPLETED
            and item.content.patch_batch is not None
        )
        and str(item.content.call_id) == row["call_id"]
    ]
    if len(contents) != 1 or contents[0].diff_artifact != ref:
        raise ValueError("差异引用不匹配")
    request = next(
        item.content
        for item in turn.items
        if isinstance(item.content, PatchBatchApprovalRequestContent)
        and str(item.content.call_id) == row["call_id"]
    )
    if request.plan.backend.manifest.workspace_scope != row["workspace_scope"]:
        raise ValueError("差异工作区错绑")
    return ref


def _tool_result_reference(row: aiosqlite.Row, turn: Turn, ref: ArtifactRef) -> ArtifactRef:
    results = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
        and str(item.content.call_id) == row["call_id"]
        and item.status == ItemStatus.COMPLETED
    ]
    if (
        len(results) != 1
        or results[0].outcome != "succeeded"
        or results[0].process is not None
        or not isinstance(results[0].output, dict)
        or results[0].output.get("artifact") != ref.model_dump(mode="json")
    ):
        raise ValueError("缺少结果引用")
    return ref
