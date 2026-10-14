"""用持久请求事实偏好保留交付阶段；不是Token准入、编辑依据或执行授权。"""

from harnessix.agent.models import Turn
from harnessix.agent.turn_budget_compaction import closing_reserve, require_request_slots

# 标准有Profile的闭环至少还需要Patch、测试、Review和最终答复四次主请求。
# 修复可能需要更多请求，因此这里只给调度偏好，绝不宣称费用已足额预付。
CLOSING_MODEL_CALLS = 4


def prefer_closing_over_investigation(turn: Turn) -> bool:
    """账本未知时不推断；真实逐请求准入仍由AgentRuntime执行。"""
    if any(attempt.usage.completeness != "complete" for attempt in turn.accounted_attempts):
        return False
    if any(record.unaccounted_request_possible for record in turn.compactions):
        return False
    completed = tuple(
        attempt
        for attempt in turn.model_attempts
        if attempt.status == "completed" and attempt.usage.completeness == "complete"
    )
    if not completed:
        return False
    last = completed[-1]
    if (
        last.usage.input_tokens is None
        or last.actual_model is None
        or last.actual_model != last.requested_model
    ):
        return False
    # 一次主请求的工具目录只能基于该请求结算时的事实。其后的摘要仍真实计费，
    # 但调度偏好延后到下一主请求结算再更新，避免压缩前后跨阈值导致目录漂移。
    later_summaries = tuple(
        record.attempt
        for record in turn.compactions
        if record.attempt is not None and record.plan.model_step > last.step
    )
    remaining = (
        turn.budget.max_tokens
        - turn.usage.total_tokens
        + sum(
            (attempt.usage.input_tokens or 0) + (attempt.usage.output_tokens or 0)
            for attempt in later_summaries
        )
    )
    request_slots = require_request_slots(turn) + len(later_summaries)
    closing_preference = CLOSING_MODEL_CALLS * (last.usage.input_tokens + closing_reserve(turn))
    return request_slots <= CLOSING_MODEL_CALLS or remaining <= closing_preference
