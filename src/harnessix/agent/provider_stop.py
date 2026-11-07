"""计费安全停止的原因归属（ADR 0123）：停止规则不变，服务方故障如实标注。

一次模型请求未完整报回用量时，产品不再发起新的付费请求。若触发停止的是服务方故障
（5xx、429、连接与超时）导致的失败尝试，以该故障的原错误码结束，否则仍为budget_exceeded。
"""

from __future__ import annotations

from collections.abc import Iterable

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Turn
from harnessix.agent.usage import ModelAttempt

# 模型适配器标为可重试的服务端失败。
SERVICE_FAILURE_CODES = frozenset(
    {"provider_provider_internal", "provider_rate_limit", "provider_transport"}
)


def service_failure_stop(attempts: Iterable[ModelAttempt]) -> KernelError | None:
    """最近一次因服务方故障失败且用量未知的尝试，按其原故障码停止。"""
    for attempt in reversed(tuple(attempts)):
        error = attempt.error
        if (
            attempt.status == "failed"
            and attempt.usage.completeness != "complete"
            and error is not None
            and error.code in SERVICE_FAILURE_CODES
        ):
            return KernelError(
                error.code,
                "Provider服务失败且该次请求用量未知；按计费安全规则不再发起付费请求",
                retryable=error.retryable,
            )
    return None


def unknown_usage_stop(turn: Turn, message: str) -> KernelError:
    """已有用量未知的主模型或摘要尝试时的停止错误。"""
    attempts = [
        attempt
        for attempt in turn.accounted_attempts
        if attempt.status != "running" and attempt.usage.completeness != "complete"
    ]
    attempts += [
        record.attempt
        for record in turn.compactions
        if record.unaccounted_request_possible and record.attempt is not None
    ]
    return service_failure_stop(attempts) or KernelError("budget_exceeded", message)
