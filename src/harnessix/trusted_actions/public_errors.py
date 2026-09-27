"""Trusted Action公开错误合同：任何内部异常在公开边界只产生稳定码与固定消息。

约束：不读取异常消息、参数值、路径或Secret。KernelError只是内部载体，
只有当前回调阶段已登记的错误码才能选择固定公开消息；绝不返回原异常。
"""

from __future__ import annotations

import asyncio
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import EffectClass

PublicActionStage = Literal["execute", "reconcile"]
PublicOutcomeKind = Literal["succeeded", "failed", "unknown", "manual_intervention"]
PublicPlanningStage = Literal["decode", "resolve", "policy"]

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
_RESOLVER_ERRORS = {
    "action_resource_invalid": "Action规范资源无效",
    "workspace_path_denied": "Workspace路径不允许访问",
    "workspace_platform_unsupported": "Workspace平台不受支持",
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


def sanitize_plan_exception(
    error: BaseException, *, stage: PublicPlanningStage = "policy"
) -> KernelError:
    """按阶段的固定码表重建公开错误；未知码/Policy错误默认失败关闭。"""

    allowed = (
        _DECODER_ERRORS if stage == "decode" else _RESOLVER_ERRORS if stage == "resolve" else {}
    )
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

    if isinstance(error, TimeoutError):
        return _RECONCILE_TIMEOUT
    if isinstance(error, asyncio.CancelledError):
        return _RECONCILE_CANCELLED
    return _RECONCILE_ERROR
