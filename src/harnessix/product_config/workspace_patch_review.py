"""Trusted Workspace Patch审批Review：物化Delivery计划并发布可分页Diff。"""

from __future__ import annotations

import json
from uuid import UUID, uuid5

from pydantic import ValidationError

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.trusted_action import WorkspacePatchTransactionPlanner
from harnessix.delivery.trusted_action_contracts import (
    WorkspacePatchInput,
    build_workspace_action_review,
)
from harnessix.trusted_actions.contracts import ActionRouteSnapshot

_ACTION_REVIEW_NAMESPACE = UUID("2beb71e9-794e-4ffb-a789-f1d19fecc9f9")


def decode_workspace_patch_input(arguments: object) -> WorkspacePatchInput:
    """以产品Action使用的同一严格合同解析Workspace Patch公共参数。"""

    try:
        return WorkspacePatchInput.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, allow_nan=False),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("trusted_action_review_invalid", "Action Review参数不符合契约") from None


class WorkspacePatchReviewProvider:
    """先保存精确事务，再按稳定身份查询优先发布同一完整Review。"""

    def __init__(
        self,
        planner: WorkspacePatchTransactionPlanner,
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
            raise KernelError("trusted_action_review_invalid", "Action Review与Route不匹配")
        proposal = decode_workspace_patch_input(route.plan.invocation.arguments)
        record = self._planner.prepare(route.plan, proposal)
        cancel.checkpoint()
        diff = build_workspace_diff(record.plan, self._planner.transactions)
        try:
            document = build_workspace_action_review(record.plan, diff.entries, diff.text)
            body = document.to_jsonl()
        except (UnicodeError, ValueError):
            raise KernelError(
                "action_review_limit", "Workspace Patch完整Diff超过审批上限"
            ) from None
        ref = await cancel.run(
            self._artifacts.publish_action_review(
                thread.thread_id,
                turn.turn_id,
                call,
                body,
                artifact_id=uuid5(
                    _ACTION_REVIEW_NAMESPACE,
                    f"{route.plan.execution.plan_id}:action_review:v1",
                ),
                workspace_scope=self._workspace_scope,
                expected_sequence=thread.sequence,
            )
        )
        cancel.checkpoint()
        return TrustedActionReview(diff_artifact=ref)
