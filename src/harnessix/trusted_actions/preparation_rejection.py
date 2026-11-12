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
    located_patch_message,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, TrustedToolBinding
from harnessix.trusted_actions.router import TrustedActionRouter

# 规划期的确定性边界拒绝及其写给模型的英文说明；Router已按固定码表归一，这里不读取内部消息。
_WORKSPACE_PATCH_PLANNING_FAILURES = {
    "delivery_metadata_unsupported": (
        "A file mode in files is not supported on this platform (Windows has no executable "
        "mode); use mode 420"
    ),
    "delivery_path_denied": (
        "A path in files cannot be written by a patch: it is the workspace root, repeats "
        "another entry, or is protected (for example .git or .env files). Fix that path and "
        "resend"
    ),
    "workspace_patch_cwd_unsupported": "apply_patch_batch only runs from the workspace root",
    # POSIX上create会补建缺失的父目录（ADR 0137）；这个码只剩不补建的平台（Windows）会出现。
    "workspace_parent_missing": (
        "A parent directory of a path in files does not exist, and on this platform "
        "apply_patch_batch does not create directories. Create the directory with the command "
        "tool (for example mkdir), then resend this call unchanged"
    ),
    "workspace_wrong_file_type": (
        "A path in files passes through an existing entry that is not a directory (for example "
        "a.txt/b.py where a.txt is a file), so nothing can be created below it. Use a path "
        "whose existing parents are directories; missing parent directories are created for you"
    ),
    "workspace_path_denied": (
        "A path in files is not a workspace path. apply_patch_batch only writes files inside "
        "the workspace, addressed relative to its root (for example src/app.py, never an "
        "absolute path, one containing .., or one that goes through a symbolic link); write "
        "files elsewhere, such as temporary directories, with the command tool"
    ),
    "workspace_platform_unsupported": "apply_patch_batch is not supported on this platform",
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
    return ToolResultContent(
        call_id=call.call_id,
        outcome="failed",
        error=AgentFailure(
            code=rejection.code, message=_WORKSPACE_PATCH_PLANNING_FAILURES[rejection.code]
        ),
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
    # SHA不符与无变化重建固定正文，不沿用实例上的消息；三类拒绝都只附加已校验的files下标。
    reported: KernelError = rejection
    if is_workspace_patch_sha_mismatch(rejection):
        reported = WorkspacePatchPreconditionError()
    elif is_workspace_patch_no_change(rejection):
        reported = WorkspacePatchNoChangeError()
    failure = AgentFailure(
        code=reported.code,
        message=located_patch_message(reported.message, rejection.file_index),
    )
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
