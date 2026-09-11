"""持久Agent状态机：根据Provider观测事实结算模型尝试用量。"""

from __future__ import annotations

from datetime import datetime

from harnessix.agent.errors import KernelError
from harnessix.agent.usage import ModelAttempt, ModelAttemptFinished, ModelUsageObserved


def settle_observation(
    attempt: ModelAttempt,
    payload: ModelUsageObserved | ModelAttemptFinished,
    occurred_at: datetime,
) -> tuple[ModelAttempt, int, int]:
    """共享累计差额规则；用途、状态阶段、预算和身份唯一性由调用方守卫。"""
    if attempt.attempt_id != payload.attempt_id or attempt.status != "running":
        raise KernelError("invalid_event", "模型尝试尚未开始或已结算")
    if isinstance(payload, ModelUsageObserved):
        try:
            payload.usage.validate_successor(attempt.usage)
            billing = payload.billing if payload.billing is not None else attempt.billing
            billing.validate_successor(attempt.billing)
            billing.validate_usage(payload.usage)
        except ValueError:
            raise KernelError("invalid_event", "模型用量或计费元数据冲突") from None
        for field in ("actual_model", "response_id"):
            before, after = getattr(attempt, field), getattr(payload, field)
            if before is not None and after is not None and before != after:
                raise KernelError("invalid_event", "尝试响应身份发生变化")
        input_delta = (payload.usage.input_tokens or 0) - (attempt.usage.input_tokens or 0)
        output_delta = (payload.usage.output_tokens or 0) - (attempt.usage.output_tokens or 0)
        return (
            attempt.model_copy(
                update={
                    "usage": payload.usage,
                    "billing": billing,
                    "actual_model": payload.actual_model or attempt.actual_model,
                    "response_id": payload.response_id or attempt.response_id,
                }
            ),
            input_delta,
            output_delta,
        )
    if payload.outcome == "completed":
        if attempt.usage.completeness != "complete":
            raise KernelError("invalid_event", "成功尝试需要完整用量")
        if attempt.actual_model is None or attempt.response_id is None:
            raise KernelError("invalid_event", "成功尝试缺少响应身份")
    return (
        attempt.model_copy(
            update={
                "status": payload.outcome,
                "error": payload.error,
                "finished_at": occurred_at,
            }
        ),
        0,
        0,
    )
