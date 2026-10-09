"""尚未进入Executor的正式拒绝：保留稳定错误并关闭不可再批准的Route。"""

from __future__ import annotations

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent, Turn, TurnStatus
from harnessix.delivery.workspace_patch_errors import (
    WorkspacePatchNoChangeError,
    WorkspacePatchPreconditionError,
    is_workspace_patch_no_change,
    is_workspace_patch_sha_mismatch,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, TrustedToolBinding
from harnessix.trusted_actions.router import TrustedActionRouter


def workspace_patch_preparation_rejection(
    router: TrustedActionRouter,
    binding: TrustedToolBinding,
    call: ToolCallContent,
    rejection: Exception,
    route: ActionRouteSnapshot,
    provider: object,
) -> ToolResultContent | None:
    """仅原内置Review的确定SHA/包含no-op拒绝，关闭原Route后才报告failed。"""

    if (
        (
            type(rejection) is not WorkspacePatchPreconditionError
            and type(rejection) is not WorkspacePatchNoChangeError
        )
        or not (
            is_workspace_patch_sha_mismatch(rejection) or is_workspace_patch_no_change(rejection)
        )
        or rejection.review_plan_id != route.plan.execution.plan_id
        or route.state != "pending_approval"
        or route.plan.binding != binding
        or (binding.source, binding.source_id, binding.tool, binding.executor_id)
        != ("builtin", "harnessix.product", "apply_patch_batch", "product.workspace-patch")
    ):
        return None
    # Review依赖Delivery/Router；延迟导入避免把产品组合引入Router启动链。
    from harnessix.product_config.workspace_patch_review import WorkspacePatchReviewProvider

    if type(provider) is not WorkspacePatchReviewProvider:
        return None
    plan_id = route.plan.execution.plan_id
    current = router.status(plan_id)
    if (
        current.plan != route.plan
        or current.state != "pending_approval"
        or router.approval(plan_id) is not None
    ):
        return None
    failure = (
        WorkspacePatchPreconditionError()
        if is_workspace_patch_sha_mismatch(rejection)
        else WorkspacePatchNoChangeError()
    ).to_failure()
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
