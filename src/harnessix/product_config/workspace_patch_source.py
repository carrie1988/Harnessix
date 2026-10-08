"""产品成功Patch归属：从认证Thread的原调用和成功结果核对原Route与事务。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemStatus, Thread, ToolCallContent, ToolResultContent
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL, WorkspacePatchTransactionPlanner
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.product_config.workspace_patch_source_contracts import WorkspacePatchSourceReference
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspacePureProgressFactory


class WorkspacePatchOwnershipError(KernelError):
    """仅本 Reader 的归属／发布事实拒绝；同码宿主控制异常不是该领域事实。"""


@dataclass(frozen=True, slots=True)
class CompletedWorkspacePatch:
    """本Thread持久成功结果的引用；尚未核对Router和Transaction，不构成授权。"""

    turn_id: UUID
    call: ToolCallContent
    result: ToolResultContent
    transaction_id: UUID


@dataclass(frozen=True, slots=True)
class OwnedWorkspacePatch:
    """沿原完整Reader验证成功来源；不读取当前文件或新增业务事实。"""

    reference: WorkspacePatchSourceReference
    record: WorkspaceTransactionRecord


def completed_workspace_patches(thread: Thread) -> tuple[CompletedWorkspacePatch, ...]:
    """只遍历本Thread的Turn，按成功Result持久顺序配对，不接纳Fork继承快照。"""
    completed = []
    for turn in thread.turns:
        calls = {
            item.content.call_id: item.content
            for item in turn.items
            if isinstance(item.content, ToolCallContent)
            and item.status is ItemStatus.COMPLETED
            and item.content.tool == WORKSPACE_PATCH_TOOL
            and item.content.tool_fingerprint is not None
        }
        for item in turn.items:
            result = item.content
            if (
                not isinstance(result, ToolResultContent)
                or item.status is not ItemStatus.COMPLETED
                or result.outcome != "succeeded"
                or result.trusted_action is None
                or result.trusted_action.state != "succeeded"
                or (call := calls.get(result.call_id)) is None
            ):
                continue
            target = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
            if result.trusted_action.plan_id == target:
                completed.append(CompletedWorkspacePatch(turn.turn_id, call, result, target))
    return tuple(completed)


def load_owned_workspace_patch(
    thread: Thread,
    target: UUID,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    *,
    checkpoint: Callable[[], None] | None = None,
    pure_progress: WorkspacePureProgressFactory | None = None,
) -> OwnedWorkspacePatch:
    """先确定本认证Thread的成功归属，再核对精确Route和published事务。"""
    matching = [
        item for item in completed_workspace_patches(thread) if item.transaction_id == target
    ]
    if len(matching) != 1:
        raise WorkspacePatchOwnershipError(
            "workspace_patch_source_not_owned", "Patch不属于本会话的成功修改"
        )
    completed = matching[0]
    if pure_progress is not None:
        route = router.status(target, checkpoint=checkpoint, pure_progress=pure_progress)
    else:
        route = (
            router.status(target)
            if checkpoint is None
            else router.status(target, checkpoint=checkpoint)
        )
    effect = completed.result.trusted_action
    if (
        effect is None
        or route.state != "succeeded"
        or effect.plan_fingerprint != route.plan.fingerprint
    ):
        raise WorkspacePatchOwnershipError(
            "workspace_patch_source_not_owned", "原Patch成功来源不一致"
        )
    proposal = WorkspacePatchInput.model_validate_json(json.dumps(completed.call.arguments))
    if route.plan.invocation.arguments != proposal.model_dump(mode="json"):
        raise WorkspacePatchOwnershipError(
            "workspace_patch_source_not_owned", "原Patch调用与Route不一致"
        )
    planner = WorkspacePatchTransactionPlanner(transactions, lambda _: Path(thread.workspace))
    if pure_progress is not None:
        record = planner.load(
            route.plan, proposal, checkpoint=checkpoint, pure_progress=pure_progress
        )
    else:
        record = (
            planner.load(route.plan, proposal)
            if checkpoint is None
            else planner.load(route.plan, proposal, checkpoint=checkpoint)
        )
    if record.state != "published":
        raise WorkspacePatchOwnershipError(
            "workspace_patch_source_not_published", "原Patch事务未完成发布"
        )
    return OwnedWorkspacePatch(
        WorkspacePatchSourceReference(
            turn_id=completed.turn_id,
            call_id=completed.call.call_id,
            transaction_id=target,
            route_fingerprint=route.plan.fingerprint,
            transaction_fingerprint=record.plan.fingerprint,
        ),
        record,
    )
