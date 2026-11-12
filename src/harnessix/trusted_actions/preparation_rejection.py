"""尚未进入Executor的正式拒绝：保留稳定错误并关闭不可再批准的Route。"""

from __future__ import annotations

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent, Turn, TurnStatus
from harnessix.delivery.workspace_patch_errors import (
    WorkspacePatchNoChangeError,
    WorkspacePatchPreconditionError,
    WorkspacePatchPreparationError,
    is_workspace_patch_no_change,
    is_workspace_patch_preparation_error,
    is_workspace_patch_sha_mismatch,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, TrustedToolBinding
from harnessix.trusted_actions.public_errors import sanitize_plan_exception
from harnessix.trusted_actions.router import TrustedActionRouter

# 父路径拒绝由规划时的快照观察抛出，不经过解析阶段的固定码表，实例上只有内部说明
# （“Workspace资源观察失败”）；投影时按同一码表换成说明原因和下一步的固定说明（ADR 0137）。
# POSIX上create会补建缺失的父目录，workspace_parent_missing只剩不补建的平台（Windows）会出现。
_PARENT_PATH_REJECTIONS = frozenset({"workspace_parent_missing", "workspace_wrong_file_type"})
_WORKSPACE_PATCH_PLANNING_FAILURES = _PARENT_PATH_REJECTIONS | {
    "delivery_metadata_unsupported",
    "delivery_path_denied",
    "workspace_patch_cwd_unsupported",
    "workspace_path_denied",
    "workspace_platform_unsupported",
}


class OfficialWorkspacePatchReview:
    """标记产品内置Patch Review；只有它的预检失败才能关闭计划，下层无需导入产品组合。"""


def workspace_patch_planning_rejection(
    binding: TrustedToolBinding,
    call: ToolCallContent,
    rejection: KernelError,
) -> ToolResultContent | None:
    """把内置Patch规划期的确定性边界拒绝投影为可修正Tool Result。"""

    if (binding.source, binding.source_id, binding.tool, binding.executor_id) != (
        "builtin",
        "harnessix.product",
        "apply_patch_batch",
        "product.workspace-patch",
    ) or rejection.code not in _WORKSPACE_PATCH_PLANNING_FAILURES:
        return None
    if rejection.code in _PARENT_PATH_REJECTIONS:
        rejection = sanitize_plan_exception(rejection, stage="resolve")
    return ToolResultContent(
        call_id=call.call_id,
        outcome="failed",
        error=AgentFailure(code=rejection.code, message=rejection.message),
    )


def workspace_patch_preparation_rejection(
    router: TrustedActionRouter,
    binding: TrustedToolBinding,
    call: ToolCallContent,
    rejection: Exception,
    route: ActionRouteSnapshot,
    provider: object,
) -> ToolResultContent | None:
    """仅原内置Review的确定性Patch拒绝，关闭原Route后才报告failed。"""

    if (
        (
            type(rejection) is not WorkspacePatchPreconditionError
            and type(rejection) is not WorkspacePatchNoChangeError
            and type(rejection) is not WorkspacePatchPreparationError
        )
        or not (
            is_workspace_patch_sha_mismatch(rejection)
            or is_workspace_patch_no_change(rejection)
            or is_workspace_patch_preparation_error(rejection)
        )
        or rejection.review_plan_id != route.plan.execution.plan_id
        or route.state != "pending_approval"
        or route.plan.binding != binding
        or (binding.source, binding.source_id, binding.tool, binding.executor_id)
        != ("builtin", "harnessix.product", "apply_patch_batch", "product.workspace-patch")
    ):
        return None
    if not isinstance(provider, OfficialWorkspacePatchReview):
        return None
    plan_id = route.plan.execution.plan_id
    current = router.status(plan_id)
    if (
        current.plan != route.plan
        or current.state != "pending_approval"
        or router.approval(plan_id) is not None
    ):
        return None
    if is_workspace_patch_sha_mismatch(rejection):
        failure = WorkspacePatchPreconditionError().to_failure()
    elif is_workspace_patch_no_change(rejection):
        failure = WorkspacePatchNoChangeError().to_failure()
    else:
        failure = AgentFailure(code=rejection.code, message=rejection.message)
    closed = router.decide(
        plan_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.REJECTED,
            actor="system.validation",
            reason=failure.code,
        ),
    )
    if closed.state != "denied" or closed.plan != route.plan:
        return None
    return ToolResultContent(call_id=call.call_id, outcome="failed", error=failure)


def rollback_preparation_rejection(
    router: TrustedActionRouter,
    binding: TrustedToolBinding,
    call: ToolCallContent,
    rejection: KernelError,
    route: ActionRouteSnapshot | None = None,
) -> ToolResultContent | None:
    """只处理已知内置回滚的授权/版本拒绝；不泛化自定义回调或执行阶段异常。"""

    if (binding.source, binding.source_id, binding.tool, binding.executor_id) != (
        "builtin",
        "harnessix.product",
        "rollback_workspace_patch",
        "product.workspace-patch-rollback",
    ):
        return None
    if route is None:
        if rejection.code not in {
            "workspace_rollback_not_owned",
            "workspace_rollback_arguments_invalid",
        }:
            return None
    else:
        if rejection.code != "workspace_rollback_conflict" or route.state != "pending_approval":
            return None
        router.decide(
            route.plan.execution.plan_id,
            ApprovalDecision(
                outcome=ApprovalOutcome.REJECTED,
                actor="system.validation",
                reason=rejection.code,
            ),
        )
    return ToolResultContent(
        call_id=call.call_id,
        outcome="failed",
        error=AgentFailure(
            code=rejection.code,
            message=rejection.message,
        ),
    )


def cancel_pending_approval(
    router: TrustedActionRouter,
    route: ActionRouteSnapshot,
    turn: Turn,
    call: ToolCallContent,
) -> ToolResultContent | None:
    """仅CANCELLING且仍未批准时证明未执行；不冒充已完成的Session审批投影。"""

    if turn.status is not TurnStatus.CANCELLING or route.state != "pending_approval":
        return None
    router.decide(
        route.plan.execution.plan_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.REJECTED,
            actor="system.cancel",
            reason="turn_cancelled",
        ),
    )
    return ToolResultContent(
        call_id=call.call_id,
        outcome="cancelled",
        error=AgentFailure(code="cancelled", message="用户取消了尚未批准的Action"),
    )
