"""产品回滚会话归属与审批Diff；根相同不构成其他会话事务的读取权限。"""

from __future__ import annotations

import json
from pathlib import Path

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemStatus, Thread, ToolCallContent, ToolResultContent, Turn
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.rollback_action import (
    WorkspaceRollbackTransactionPlanner,
    decode_workspace_rollback_input,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL, WorkspacePatchTransactionPlanner
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.product_config.workspace_patch_review import publish_workspace_review
from harnessix.trusted_actions.contracts import ActionRouteSnapshot
from harnessix.trusted_actions.router import TrustedActionRouter


def authorize_workspace_rollback(
    thread: Thread,
    call: ToolCallContent,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
) -> None:
    """原认证Thread必须包含配对成功效果，先验归属再访问原计划与Blob。"""

    target = decode_workspace_rollback_input(call.arguments).transaction_id
    for turn in thread.turns:
        for item in turn.items:
            original = item.content
            if (
                not isinstance(original, ToolCallContent)
                or item.status is not ItemStatus.COMPLETED
                or original.tool != WORKSPACE_PATCH_TOOL
                or trusted_action_invocation_id(thread.thread_id, turn.turn_id, original) != target
            ):
                continue
            route = router.status(target)
            success = any(
                isinstance(result.content, ToolResultContent)
                and result.status is ItemStatus.COMPLETED
                and result.content.call_id == original.call_id
                and result.content.outcome == "succeeded"
                and result.content.trusted_action is not None
                and result.content.trusted_action.plan_id == target
                and result.content.trusted_action.plan_fingerprint == route.plan.fingerprint
                and result.content.trusted_action.state == "succeeded"
                for result in turn.items
            )
            if not success or route.state != "succeeded":
                break
            # 沿原Patch校验完整Route/Transaction，不能仅接受模型输出中的UUID。
            proposal = WorkspacePatchInput.model_validate_json(json.dumps(original.arguments))
            if route.plan.invocation.arguments != proposal.model_dump(mode="json"):
                break
            WorkspacePatchTransactionPlanner(transactions, lambda _: Path(thread.workspace)).load(
                route.plan, proposal
            )
            return
    raise KernelError("workspace_rollback_not_owned", "该Patch不属于本会话的成功修改")


class WorkspaceRollbackReviewProvider:
    """以新事务完整逆向Diff进入原Session Artifact和人工批准链。"""

    def __init__(
        self,
        planner: WorkspaceRollbackTransactionPlanner,
        artifacts: SQLiteArtifactStore,
        *,
        workspace_scope: str,
    ) -> None:
        self._planner = planner
        self._artifacts = artifacts
        self._workspace_scope = workspace_scope

    async def review(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> TrustedActionReview:
        cancel.checkpoint()
        if (
            route.state != "pending_approval"
            or call.tool != route.plan.binding.tool
            or route.plan.execution.plan_id
            != trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        ):
            raise KernelError("trusted_action_review_invalid", "回滚Review与原Route不匹配")
        proposal = decode_workspace_rollback_input(route.plan.invocation.arguments)
        record = self._planner.prepare(route.plan, proposal)
        return await publish_workspace_review(
            route,
            thread,
            turn,
            call,
            cancel,
            record,
            self._planner.transactions,
            self._artifacts,
            self._workspace_scope,
        )
