"""尚未进入Executor的正式拒绝：保留稳定错误并关闭不可再批准的Route。"""

from __future__ import annotations

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent, Turn, TurnStatus
from harnessix.agent.publication import PROTECTION_LIMIT_FAILURES
from harnessix.delivery.workspace_patch_errors import (
    WorkspacePatchNoChangeError,
    WorkspacePatchPreconditionError,
    WorkspacePatchPreparationError,
    is_workspace_patch_no_change,
    is_workspace_patch_preparation_error,
    is_workspace_patch_sha_mismatch,
    located_patch_message,
    unapplied_batch_notice,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass
from harnessix.tools.contracts import ReadToolError
from harnessix.trusted_actions.contracts import (
    UNSTARTED_PLAN_CODE,
    ActionRouteSnapshot,
    TrustedToolBinding,
)
from harnessix.trusted_actions.operation_router import end_unstarted_plan
from harnessix.trusted_actions.router import TrustedActionRouter

# 规划期的确定性边界拒绝及其写给模型的英文说明；Router已按固定码表归一，这里不读取内部消息。
_WORKSPACE_PATCH_PLANNING_FAILURES = {
    "delivery_metadata_unsupported": (
        "A file mode in files is not supported on this platform (Windows has no executable "
        "mode); use mode 420"
    ),
    "delivery_path_denied": (
        "A path in files cannot be written by a patch: it is the workspace root, repeats "
        "another entry, or is protected (for example .git, .env, .ssh or id_rsa), or on "
        "Windows is an 8.3 short name such as GIT~1 (use the full name). Fix that path and "
        "resend"
    ),
    "workspace_patch_cwd_unsupported": "apply_patch_batch only runs from the workspace root",
    # create会补建缺失的父目录（POSIX自ADR 0137，Windows自其第2版）；
    # 这个码只剩没有补建能力的端口会出现。
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
    rejection: Exception,
) -> ToolResultContent | None:
    """把内置Patch规划期的确定性边界拒绝投影为可修正Tool Result；其他可信操作与其他异常返回None。"""

    code = _planning_failure_code(rejection)
    if (binding.source, binding.source_id, binding.tool, binding.executor_id) != (
        "builtin",
        "harnessix.product",
        "apply_patch_batch",
        "product.workspace-patch",
    ) or code not in _WORKSPACE_PATCH_PLANNING_FAILURES:
        return None
    # 码表不含出错的是哪一项（见上），但整批未应用是确定的事实，一并告诉模型。
    message = _WORKSPACE_PATCH_PLANNING_FAILURES[code]
    return ToolResultContent(
        call_id=call.call_id,
        outcome="failed",
        error=AgentFailure(
            code=code, message=f"{message}. {unapplied_batch_notice(_file_count(call))}"
        ),
    )


def _planning_failure_code(rejection: Exception) -> str | None:
    """规划失败的公开码。POSIX规划按写目标策略观察要写的路径，保护名称（.ssh、.env.*、id_rsa等）
    以ReadToolError("path_denied")失败：它与Delivery层的保护路径同属“这个路径不能写”。
    此前未转换，整轮以runtime_error中断（2026-11-17 NL2RepoBench冒烟：wsgidav新建
    sample_bogo_server.key）。密钥与证书文件自ADR 0172起可写，不再走到这里。"""
    if isinstance(rejection, KernelError):
        return rejection.code
    if isinstance(rejection, ReadToolError) and rejection.code == "path_denied":
        return "delivery_path_denied"
    return None


def _file_count(call: ToolCallContent) -> int:
    files = call.arguments.get("files")
    return len(files) if isinstance(files, list) else 1


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
    # SHA不符与无变化重建固定正文，不沿用实例上的消息；三类拒绝都只附加已校验的files下标
    # 与整批未应用的说明。
    reported: KernelError = rejection
    if is_workspace_patch_sha_mismatch(rejection):
        reported = WorkspacePatchPreconditionError()
    elif is_workspace_patch_no_change(rejection):
        reported = WorkspacePatchNoChangeError()
    failure = AgentFailure(
        code=reported.code,
        message=located_patch_message(reported.message, rejection.file_index, _file_count(call)),
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


# Review存档存不下（ADR 0163）或太大、保护检查做不完（ADR 0174）时动作没有执行；写给模型的英文说明。
_UNSAVED_REVIEW = {
    "artifact_quota_exceeded": (
        "Artifact storage is full, so the review of this action could not be saved and the "
        "action was not run; nothing changed. Storage is freed as old archives expire"
    ),
    **dict.fromkeys(
        PROTECTION_LIMIT_FAILURES,
        "The review of this action was too large to check for protected values before it could "
        "be shown for approval, so the action was not run; nothing changed. Split it into "
        "smaller actions",
    ),
}


def preparation_rejection_result(
    router: TrustedActionRouter,
    binding: TrustedToolBinding,
    call: ToolCallContent,
    rejection: KernelError,
    route: ActionRouteSnapshot | None = None,
) -> ToolResultContent | None:
    """尚未执行的准备期拒绝：任意Action的Review存档存不下或检查做不完，或内置回滚的授权/版本拒绝。"""

    message = _UNSAVED_REVIEW.get(rejection.code)
    if message is None or route is None:
        return rollback_preparation_rejection(router, binding, call, rejection, route)
    plan_id = route.plan.execution.plan_id
    current = router.status(plan_id)
    if (
        route.state != "pending_approval"
        or current.plan != route.plan
        or current.state != "pending_approval"
        or router.approval(plan_id) is not None
    ):
        return None
    closed = router.decide(
        plan_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.REJECTED, actor="system.validation", reason=rejection.code
        ),
    )
    if closed.state != "denied" or closed.plan != route.plan:
        return None
    return ToolResultContent(
        call_id=call.call_id,
        outcome="failed",
        error=AgentFailure(code=rejection.code, message=message),
    )


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


def close_unstarted_route(
    router: TrustedActionRouter,
    route: ActionRouteSnapshot,
    turn: Turn,
    call: ToolCallContent,
) -> ToolResultContent | None:
    """本轮收尾时，Route证明尚未开始的调用记为未执行，并关闭Route，不留可再批准或可执行的计划。

    待审批：系统拒绝（用户取消、时限已过，或本轮因其他原因结束）；已批准未Claim：Executor从未
    调用，以确定失败结束。此前都记为未知效果，整轮以uncertain_effect中断（Beta Issue #39、#37）。
    其他状态返回None，由调用方照常核对。
    """

    plan_id = route.plan.execution.plan_id
    reason, failure = _turn_ending(turn)
    if route.state == "pending_approval":
        decision = ApprovalDecision(
            outcome=ApprovalOutcome.REJECTED, actor="system.cancel", reason=reason
        )
        if router.decide(plan_id, decision).state != "denied":
            raise KernelError("action_route_conflict", "尚未批准的Action未能关闭")
    elif route.state == "ready":
        end_unstarted_plan(router, plan_id)
        failure = failure.model_copy(update={"message": _UNSTARTED_MESSAGE})
    else:
        return None
    return ToolResultContent(call_id=call.call_id, outcome="cancelled", error=failure)


_UNSTARTED_MESSAGE = "本轮在这次调用开始前结束，调用没有执行"


def _turn_ending(turn: Turn) -> tuple[str, AgentFailure]:
    """本轮为什么结束：用户取消、时限已过，或其他原因（另一个调用失败、宿主中断等）。"""
    if turn.status is TurnStatus.CANCELLING:
        return "turn_cancelled", AgentFailure(
            code="cancelled", message="用户取消了尚未批准的Action"
        )
    if remaining_seconds(turn) <= 0:
        return "turn_time_budget_exceeded", AgentFailure(
            code="time_budget_exceeded", message="审批超过本轮时限，Action没有执行"
        )
    return "turn_ended", AgentFailure(code=UNSTARTED_PLAN_CODE, message=_UNSTARTED_MESSAGE)


def unplanned_result(call: ToolCallContent) -> ToolResultContent | None:
    """本轮收尾时写调用连Route都没有：从未规划，Executor不可能被调用，记为未执行（Issue #37）。

    只读调用不会被记为未知效果，照常按本轮错误收尾，返回None。
    """
    if call.effect_class is EffectClass.READ_ONLY:
        return None
    failure = AgentFailure(code=UNSTARTED_PLAN_CODE, message=_UNSTARTED_MESSAGE)
    return ToolResultContent(call_id=call.call_id, outcome="cancelled", error=failure)
