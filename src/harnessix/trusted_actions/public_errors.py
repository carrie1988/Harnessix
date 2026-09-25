"""Trusted Action公开错误合同：任何内部异常在公开边界只产生稳定码与固定消息。

约束：本模块不读取异常内容、参数值、路径或Secret；已具备公开纪律的
KernelError原样传播，其余异常一律收敛为不含细节的稳定结果。
"""

from __future__ import annotations

import asyncio
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import EffectClass

PublicActionStage = Literal["execute", "reconcile"]
PublicOutcomeKind = Literal["succeeded", "failed", "unknown", "manual_intervention"]

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


def sanitize_plan_exception(error: BaseException) -> KernelError:
    """把计划阶段的任意异常收敛为公开KernelError；KernelError保持原样。"""

    if isinstance(error, KernelError):
        return error
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
