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


def _unbilled_service_failure(attempt: ModelAttempt) -> bool:
    """因服务方故障失败、且用量未完整报回的尝试。"""
    return (
        attempt.status == "failed"
        and attempt.usage.completeness != "complete"
        and attempt.error is not None
        and attempt.error.code in SERVICE_FAILURE_CODES
    )


def service_failure_stop(attempts: Iterable[ModelAttempt]) -> KernelError | None:
    """最近一次因服务方故障失败且用量未知的尝试，按其原故障码停止。"""
    for attempt in reversed(tuple(attempts)):
        if _unbilled_service_failure(attempt) and attempt.error is not None:
            return KernelError(
                attempt.error.code,
                "Provider服务失败且该次请求用量未知；按计费安全规则不再发起付费请求",
                retryable=attempt.error.retryable,
            )
    return None


def service_failures_within(turn: Turn, limit: int) -> bool:
    """本Turn用量未知的尝试是否全部来自服务方故障，且最近连续的此类失败不超过limit次（ADR 0144）。

    网络中断、限流和服务端错误是一时的；交互模式下允许在这个次数内继续请求。其他原因造成的用量未知
    （例如响应正常结束却没有报回用量）不在此列，仍按计费安全规则停止。
    """
    settled = [attempt for attempt in turn.accounted_attempts if attempt.status != "running"]
    unknown = [attempt for attempt in settled if attempt.usage.completeness != "complete"]
    unaccounted = [record for record in turn.compactions if record.unaccounted_request_possible]
    if limit <= 0 or not (unknown or unaccounted):
        return False
    if not all(map(_unbilled_service_failure, unknown)) or any(
        record.attempt is None or not _unbilled_service_failure(record.attempt)
        for record in unaccounted
    ):
        return False
    consecutive = 0
    for attempt in reversed([item for item in turn.model_attempts if item.status != "running"]):
        if not _unbilled_service_failure(attempt):
            break
        consecutive += 1
    return consecutive <= limit


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
