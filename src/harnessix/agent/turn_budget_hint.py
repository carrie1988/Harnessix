"""按账本实际用量推算剩余可负担请求数；只用于模型可见提示，不参与准入或预付。"""

from __future__ import annotations

from harnessix.agent.models import Turn
from harnessix.agent.turn_budget_compaction import require_request_slots

MAX_AFFORDABLE_REQUESTS = 1000


def affordable_request_hint(turn: Turn) -> int | None:
    """以最近一次完整成功请求的实际输入＋输出为单次尺寸；无可信用量时不推断。"""
    completed = tuple(
        attempt
        for attempt in turn.model_attempts
        if attempt.status == "completed" and attempt.usage.completeness == "complete"
    )
    if not completed or any(
        attempt.usage.completeness != "complete" for attempt in turn.accounted_attempts
    ):
        return None
    last = completed[-1].usage
    if last.input_tokens is None or last.output_tokens is None:
        return None
    size = max(1, last.input_tokens + last.output_tokens)
    remaining = max(0, turn.budget.max_tokens - turn.usage.total_tokens)
    return min(remaining // size, require_request_slots(turn), MAX_AFFORDABLE_REQUESTS)
