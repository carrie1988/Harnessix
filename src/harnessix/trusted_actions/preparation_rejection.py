"""尚未进入Executor的正式拒绝：保留稳定错误并关闭不可再批准的Route。"""

from __future__ import annotations

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent, Turn, TurnStatus
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, TrustedToolBinding
from harnessix.trusted_actions.router import TrustedActionRouter


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
