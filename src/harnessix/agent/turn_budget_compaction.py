"""Turn 剩余额度派生窗口策略；不调用模型或改变原预算。"""

from uuid import UUID, uuid5

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


def pressure_history_allocation(turn: Turn, fixed_input: int) -> int:
    """剩余累计额度的重复输入目标；只是规划偏好，不是丢弃固定历史的授权。"""
    remaining = turn.budget.max_tokens - turn.usage.total_tokens
    horizon = min(3, require_request_slots(turn))
    return remaining // horizon - fixed_input - closing_reserve(turn)


def pressure_policy(
    turn: Turn, config: CompactionRuntimeConfig, fixed_input: int, history_input: int
) -> CompactionPolicy | None:
    horizon = min(3, require_request_slots(turn))
    allocation = pressure_history_allocation(turn, fixed_input)
    # 先判断均摊压力；规划器再在用户锚点和近期完整组的保留边界上选择紧凑目标。
    target = min(
        config.policy.target_history_tokens, history_input - config.policy.min_savings_tokens
    )
    reserve = min(config.policy.summary_reserve_tokens, max(256, target // 4))
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


def pressure_policies(
    turn: Turn, config: CompactionRuntimeConfig, fixed_input: int, history_input: int
) -> tuple[CompactionPolicy, ...]:
    """只读选择有界摘要容量；不因大段保留历史而预留不必要的大摘要。

    优先原容量；只有规划、摊销或完整预付不成立才尝试较小容量。最多三个
    纯规划候选，不是模型请求重试，也不缩减原用户锚点和近期完整组。
    """
    initial = pressure_policy(turn, config, fixed_input, history_input)
    if initial is None:
        return ()
    if config.max_summary_output_tokens < 256:
        return (initial,)
    # UUID 文本及稳定摘要 Item 的封装长度固定；正式Plan仍使用其原真实身份。
    wrapper = _summary_wrapper_bytes(UUID(int=0))
    reserves = tuple(
        dict.fromkeys(
            (
                initial.summary_reserve_tokens,
                *(
                    min(
                        initial.summary_reserve_tokens,
                        wrapper + 4 * min(config.max_summary_output_tokens, capacity),
                    )
                    for capacity in (512, 256)
                ),
            )
        )
    )
    return tuple(
        CompactionPolicy.model_validate({**initial.model_dump(), "summary_reserve_tokens": reserve})
        for reserve in reserves
    )


def summary_request_item(planned: PreparedCompaction) -> Item:
    return Item(
        item_id=uuid5(planned.plan.compaction_id, "harnessix.compaction-request/v1"),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text=planned.summary_source),
    )


def _summary_wrapper_bytes(compaction_id: UUID) -> int:
    wrapper = compaction_summary_item(CompactionSummary(compaction_id=compaction_id, text="x"))
    return len(history_document(wrapper).encode()) - 1


def summary_output_limit(config: CompactionRuntimeConfig, planned: PreparedCompaction) -> int:
    wrapper_bytes = _summary_wrapper_bytes(planned.plan.compaction_id)
    return min(
        config.max_summary_output_tokens,
        max(0, (planned.plan.policy.summary_reserve_tokens - wrapper_bytes) // 4),
    )


def pressure_summary_instructions(
    instructions: str, config: CompactionRuntimeConfig, planned: PreparedCompaction
) -> str:
    """把实际输出容量明确交给摘要模型；精确UTF-8与完整投影验证仍由Runtime执行。"""
    limit = summary_output_limit(config, planned)
    return (
        instructions
        + f"\nOutput capacity: at most {limit} tokens. Finish normally well before that limit. "
        "Use one compact paragraph of factual phrases, not headings or a narrative. "
        "Keep exact identifiers, observed outcomes, unresolved work and uncertain effects. "
        "Omit copied source code, repeated logs and speculation; do not invent absent facts."
    )


def require_context_unchanged(before: PreparedContext, after: PreparedContext) -> None:
    """摘要期间只允许历史长度变化；指令、来源和权限事实必须重新求证。"""
    old, new = before.inspection.model_dump(), after.inspection.model_dump()
    fields = ("instruction_fingerprint", "limits", "fragments", "sources", "consistency")
    if any(old.get(field) != new.get(field) for field in fields):
        raise KernelError("context_compaction_source_changed", "摘要期间Context指令或来源发生漂移")
