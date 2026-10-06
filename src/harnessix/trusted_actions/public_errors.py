"""Trusted Action公开错误合同：任何内部异常在公开边界只产生稳定码与固定消息。

约束：不读取异常消息、参数值、路径或Secret。KernelError只是内部载体，
只有当前回调阶段已登记的错误码才能选择固定公开消息；绝不返回原异常。
"""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import EffectClass
from harnessix.trusted_actions.outcome_validation import (
    OutcomeRejectionReason,
    OutcomeValidationError,
)

PublicActionStage = Literal["execute", "reconcile"]
PublicOutcomeKind = Literal["succeeded", "failed", "unknown", "manual_intervention"]
PublicPlanningStage = Literal["decode", "resolve", "policy", "preparation"]
PublicGatewayStage = Literal["context", "review", "output"]

_EXECUTE_TIMEOUT_FAILURE = "executor_timeout"
_EXECUTE_TIMEOUT_UNKNOWN = "write_effect_timeout_unknown"
_EXECUTE_CANCEL_FAILURE = "executor_cancelled"
_EXECUTE_CANCEL_UNKNOWN = "cancelled_write_effect_unknown"
_EXECUTE_FAILURE = "executor_error"
_EXECUTE_WRITE_UNKNOWN = "unexpected_write_error"
_UNCERTAIN_EFFECT = "uncertain_external_effect"

_RECONCILE_TIMEOUT = "reconciliation_timeout"
_RECONCILE_CANCELLED = "reconciliation_cancelled"
_RECONCILE_ERROR = "reconciliation_error"

_PLAN_FAILED = "action_plan_failed"
_PLAN_MESSAGE = "Action计划阶段失败；内部原因不公开"

# 这里只登记实际内置解码器/Resolver可能抛出的码，不接受扩展动态注册消息。
# 同一码的多个内部拒绝原因合并为固定公开消息，避免未来拼接路径时越界。
_DECODER_ERRORS = {
    "tool_invalid_arguments": "Action参数不符合Trusted Tool契约",
    "trusted_tool_schema_invalid": "Trusted Tool Schema不是规范JSON对象",
}
_PREPARATION_ERRORS = {
    "action_preparation_invalid": "Action可信准备结果不符合契约",
    "action_preparation_workspace_changed": "可信准备后Workspace完整观察已变化",
}
_RESOLVER_ERRORS = {
    "action_resource_invalid": "Action规范资源无效",
    "workspace_path_denied": "Workspace路径不允许访问",
    "workspace_platform_unsupported": "Workspace平台不受支持",
    "workspace_rollback_arguments_invalid": "Patch回滚参数不符合契约",
    "workspace_rollback_source_invalid": "Patch回滚来源不符合条件",
    "workspace_patch_arguments_invalid": "Workspace Patch参数类型不一致",
    "workspace_patch_cwd_unsupported": "Workspace Patch只支持根级cwd",
    "delivery_path_denied": "Workspace Patch路径不允许写入",
    "git_workspace_mismatch": "Git Push与规划Workspace不一致",
    "git_repository_changed": "Git仓库绑定已经变化",
    "git_push_input_invalid": "Git Push输入无效",
    "process_profile_arguments_invalid": "Process Profile参数类型不一致",
    "process_profile_context_mismatch": "Process Profile规划上下文不匹配",
    "eval_test_profile_arguments_invalid": "Eval测试Profile参数不一致",
    "eval_test_profile_context_mismatch": "Eval测试Profile规划上下文不匹配",
}

# 只保留实际Owner的固定发布分类；Provider不能用Resolver码扩大公开权限。
_ARTIFACT_PUBLICATION_ERRORS = {
    "artifact_invalid": "Action Artifact不符合公开契约",
    "artifact_corrupt": "Action Artifact校验失败",
    "artifact_quota_exceeded": "Action Artifact配额不足",
    "artifact_runtime_required": "Action Artifact发布需要活跃Session",
    "artifact_store_mismatch": "Action Artifact发布器绑定不一致",
    "sequence_conflict": "Action Artifact发布时Session已变化",
    "approval_mismatch": "Action Artifact发布缺少匹配批准",
    "tool_output_too_large": "Action Artifact引用超过输出上限",
}
# 仅固定公开保护拒绝码可在效果已验真的恢复中省略正文；不授权任何Artifact。
PUBLIC_OUTPUT_REJECTIONS = frozenset(
    (
        "public_output_secret_leak",
        "public_output_secret_unavailable",
        "public_output_limit",
        "public_output_timeout",
        "public_output_protection_failed",
        "public_output_binary_capability_missing",
    )
)
_GATEWAY_ERRORS: dict[PublicGatewayStage, dict[str, str]] = {
    "context": {
        "workspace_rollback_not_owned": "该Patch不属于本会话的成功修改",
        "workspace_rollback_arguments_invalid": "Patch回滚参数不符合契约",
    },
    "review": {
        **_ARTIFACT_PUBLICATION_ERRORS,
        "workspace_rollback_conflict": "Patch回滚目标存在后续改动",
        "workspace_rollback_source_invalid": "Patch回滚来源不符合条件",
        "trusted_action_review_invalid": "Action审批预览不符合契约",
        "action_review_limit": "Action审批预览超过上限",
    },
    "output": {
        **_ARTIFACT_PUBLICATION_ERRORS,
        **dict.fromkeys(PUBLIC_OUTPUT_REJECTIONS, "公开结果未通过保护校验"),
        "trusted_action_output_mismatch": "Action输出与审计终态不匹配",
        "trusted_action_output_limit": "Action输出投影超过资源上限",
        "trusted_action_output_timeout": "Action输出投影超时",
        "trusted_action_secret_unavailable": "Action输出缺少匹配的Secret保护能力",
        "trusted_action_secret_leak": "Action输出包含受保护Secret",
        "process_not_terminal": "Action Process尚未形成终态输出",
        "process_output_corrupt": "Action Process终态输出校验失败",
    },
}
_GATEWAY_DEFAULT_MESSAGES: dict[PublicGatewayStage, str] = {
    "context": "Action规划上下文构造失败；内部原因不公开",
    "review": "Action审批预览生成失败；内部原因不公开",
    "output": "Action终态输出投影失败；内部原因不公开",
}


def sanitize_gateway_exception(error: Exception, *, stage: PublicGatewayStage) -> KernelError:
    """仅归一回调异常，不改变动作事实；调用方必须先传播取消信号。"""

    if isinstance(error, KernelError) and type(error.code) is str:
        message = _GATEWAY_ERRORS[stage].get(error.code)
        if message is not None:
            return KernelError(error.code, message)
    return KernelError(f"trusted_action_{stage}_failed", _GATEWAY_DEFAULT_MESSAGES[stage])


def sanitize_plan_exception(
    error: BaseException, *, stage: PublicPlanningStage = "policy"
) -> KernelError:
    """按阶段的固定码表重建公开错误；未知码/Policy错误默认失败关闭。"""

    allowed = {
        "decode": _DECODER_ERRORS,
        "resolve": _RESOLVER_ERRORS,
        "preparation": _PREPARATION_ERRORS,
        "policy": {},
    }[stage]
    if isinstance(error, KernelError) and type(error.code) is str:
        message = allowed.get(error.code)
        if message is not None:
            # 不继承message、args、retryable、异常链或其他内部附加字段。
            return KernelError(error.code, message)
    return KernelError(_PLAN_FAILED, _PLAN_MESSAGE)


def execute_exception_outcome(
    error: BaseException, effect_class: EffectClass
) -> tuple[PublicOutcomeKind, str]:
    """把执行期异常映射为公开（kind, error_code）；不读取异常内容。"""

    read_only = effect_class is EffectClass.READ_ONLY
    if type(error) is OutcomeValidationError:
        reason = _rejection_reason(error)
        code = f"executor_output_{reason}" if read_only else f"write_output_{reason}_unknown"
        return ("failed" if read_only else "unknown"), code
    if isinstance(error, UncertainEffectError):
        return "unknown", _UNCERTAIN_EFFECT
    if isinstance(error, TimeoutError):
        return (
            ("failed" if read_only else "unknown"),
            _EXECUTE_TIMEOUT_FAILURE if read_only else _EXECUTE_TIMEOUT_UNKNOWN,
        )
    if isinstance(error, asyncio.CancelledError):
        return (
            ("failed" if read_only else "unknown"),
            _EXECUTE_CANCEL_FAILURE if read_only else _EXECUTE_CANCEL_UNKNOWN,
        )
    return (
        ("failed" if read_only else "unknown"),
        _EXECUTE_FAILURE if read_only else _EXECUTE_WRITE_UNKNOWN,
    )


def reconcile_exception_code(error: BaseException) -> str:
    """把对账期异常映射为公开error_code；不读取异常内容。"""

    if type(error) is OutcomeValidationError:
        reason = _rejection_reason(error)
        return f"reconciliation_output_{reason}"
    if isinstance(error, TimeoutError):
        return _RECONCILE_TIMEOUT
    if isinstance(error, asyncio.CancelledError):
        return _RECONCILE_CANCELLED
    return _RECONCILE_ERROR


def _rejection_reason(error: OutcomeValidationError) -> OutcomeRejectionReason:
    """只允许内核有限原因，拒绝扩展属性类型或伪造的任意分类字符串。"""

    values = vars(error)
    reason = values.get("reason") if type(values) is dict else None
    if type(reason) is not str or reason not in {"invalid", "limit", "timeout"}:
        return "invalid"
    return cast(OutcomeRejectionReason, reason)
