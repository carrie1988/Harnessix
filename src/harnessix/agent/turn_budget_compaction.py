"""Turn 剩余额度派生窗口策略；不调用模型或改变原预算。"""

from uuid import uuid5

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Item, ItemStatus, TextContent, Turn, TurnStatus
from harnessix.context.compaction import PreparedCompaction
from harnessix.context.compaction_contracts import CompactionPolicy, CompactionSummary
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.contracts import PreparedContext
from harnessix.context.tool_result_view import history_document


def require_request_slots(turn: Turn, slots: int = 1) -> int:
    attempts = turn.accounted_attempts
    if any(a.status != "running" and a.usage.completeness != "complete" for a in attempts) or any(
        c.unaccounted_request_possible for c in turn.compactions
    ):
        raise KernelError("budget_exceeded", "已有未知模型费用，禁止下一付费请求")
    used = turn.model_steps + len(attempts) - len({a.step for a in turn.model_attempts})
    if turn.status == TurnStatus.CALLING_MODEL and not any(
        a.step == turn.model_steps for a in turn.model_attempts
    ):
        used -= 1
    if used + slots > turn.budget.max_steps:
        raise KernelError("budget_exceeded", "主模型、摘要及重试尝试的总请求预算耗尽")
    return turn.budget.max_steps - used


def closing_reserve(turn: Turn) -> int:
    per_step = max(1, turn.budget.max_tokens // turn.budget.max_steps)
    return min(turn.budget.max_output_chars, per_step)


def pressure_policy(
    turn: Turn, config: CompactionRuntimeConfig, fixed_input: int, history_input: int
) -> CompactionPolicy | None:
    remaining = turn.budget.max_tokens - turn.usage.total_tokens
    horizon = min(3, require_request_slots(turn))
    allocation = remaining // horizon - fixed_input - closing_reserve(turn)
    # 均摊预算只判断压力；窗口仍须容纳原用户和近期闭合组，不能强压成不可规划的目标。
    target = min(
        config.policy.target_history_tokens, history_input - config.policy.min_savings_tokens
    )
    reserve = min(config.policy.summary_reserve_tokens, 1024, max(256, target // 4))
    if horizon < 2 or target < 512 or target <= reserve or history_input <= allocation:
        return None
    return CompactionPolicy.model_validate(
        {
            **config.policy.model_dump(),
            "target_history_tokens": target,
            "summary_reserve_tokens": reserve,
            "retain_recent_groups": max(2, config.policy.retain_recent_groups),
        }
    )


def summary_request_item(planned: PreparedCompaction) -> Item:
    return Item(
        item_id=uuid5(planned.plan.compaction_id, "harnessix.compaction-request/v1"),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text=planned.summary_source),
    )


def summary_output_limit(config: CompactionRuntimeConfig, planned: PreparedCompaction) -> int:
    wrapper = compaction_summary_item(
        CompactionSummary(compaction_id=planned.plan.compaction_id, text="x")
    )
    wrapper_bytes = len(history_document(wrapper).encode()) - 1
    return min(
        config.max_summary_output_tokens,
        max(0, (planned.plan.policy.summary_reserve_tokens - wrapper_bytes) // 4),
    )


def require_context_unchanged(before: PreparedContext, after: PreparedContext) -> None:
    """摘要期间只允许历史长度变化；指令、来源和权限事实必须重新求证。"""
    old, new = before.inspection.model_dump(), after.inspection.model_dump()
    fields = ("instruction_fingerprint", "limits", "fragments", "sources", "consistency")
    if any(old.get(field) != new.get(field) for field in fields):
        raise KernelError("context_compaction_source_changed", "摘要期间Context指令或来源发生漂移")
