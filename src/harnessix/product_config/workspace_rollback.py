"""产品回滚会话归属与审批Diff；根相同不构成其他会话事务的读取权限。"""

from __future__ import annotations

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.rollback_action import (
    WorkspaceRollbackTransactionPlanner,
    decode_workspace_rollback_input,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.workspace_patch_review import publish_workspace_review
from harnessix.product_config.workspace_patch_source import load_owned_workspace_patch
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
    try:
        load_owned_workspace_patch(thread, target, router, transactions)
    except KernelError as error:
        if error.code == "workspace_patch_source_not_owned":
            raise KernelError(
                "workspace_rollback_not_owned", "该Patch不属于本会话的成功修改"
            ) from None
        if error.code == "workspace_patch_source_not_published":
            raise KernelError(
                "workspace_rollback_source_invalid", "Patch回滚来源未完成发布"
            ) from None
        raise


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
        record = self._planner.prepare(
            route.plan, proposal, checkpoint=parent_cancel_checkpointer(cancel.checkpoint)
        )
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
