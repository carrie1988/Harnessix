"""Turn 剩余额度派生窗口策略；不调用模型或改变原预算。"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid5

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    Item,
    ItemStarted,
    ItemStatus,
    TextContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.provider_stop import service_failures_within, unknown_usage_stop
from harnessix.agent.usage import ModelAttemptStarted
from harnessix.context.compaction import PreparedCompaction
from harnessix.context.compaction_contracts import CompactionPolicy, CompactionSummary
from harnessix.context.compaction_projection import (
    SUMMARY_PROJECTION_HEADER,
    compaction_summary_item,
)
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.contracts import PreparedContext
from harnessix.context.tool_result_view import history_document


@dataclass(frozen=True, slots=True)
class UnknownUsagePolicy:
    """已有尝试未完整报回用量时，是否还允许发起下一次付费请求（ADR 0123、0144）。

    always：一律按预留额继续，用于无人值守运行和不计费的本地端点。
    service_failure_retries：用量未知只因服务方故障（5xx、429、连接与超时）时，最近连续不超过这么多次
    失败仍按预留额继续；0表示一次也不允许。两者都不满足时停止，交给用户决定。
    """

    always: bool = False
    service_failure_retries: int = 0

    def __post_init__(self) -> None:
        retries = self.service_failure_retries
        if type(self.always) is not bool or type(retries) is not int or not 0 <= retries <= 32:
            raise KernelError("invalid_runtime_configuration", "未知用量策略无效")

    def continues(self, turn: Turn) -> bool:
        return self.always or service_failures_within(turn, self.service_failure_retries)


# 默认策略：用量未知即停止。
STOP_ON_UNKNOWN_USAGE = UnknownUsagePolicy()


def unknown_usage_reservation(turn: Turn) -> int:
    """用量未知的尝试按本Turn已知的单次最大消耗保守预留；没有已知用量时无从估计，记0。"""

    attempts = turn.accounted_attempts
    known = (
        (a.usage.input_tokens or 0) + (a.usage.output_tokens or 0)
        for a in attempts
        if a.usage.completeness == "complete"
    )
    unknown = sum(a.status != "running" and a.usage.completeness != "complete" for a in attempts)
    return unknown * max(known, default=0)


def require_reserved_unknown_usage(turn: Turn) -> None:
    """允许继续时，未知用量的预留一旦耗尽Token预算就停止。

    停止的原因是预算（已用量加上为那次失败请求预留的用量），以budget_exceeded结束并给出数字。
    此前沿用那次早已恢复的服务方故障的错误码与“计费安全规则”的说明：0.10.0b38上一次传输
    错误恢复后又成功请求了42次，最后仍报那次传输错误，用户以为恢复被它中断（Beta Issue #32）。
    """

    reservation = unknown_usage_reservation(turn)
    if turn.usage.total_tokens + reservation >= turn.budget.max_tokens:
        raise KernelError(
            "budget_exceeded",
            f"本轮Token预算将用完：已用{turn.usage.total_tokens}，另为用量未知的失败请求预留"
            f"{reservation}，合计达到上限{turn.budget.max_tokens}；提高预算后可在同一会话续做",
        )


def require_request_slots(turn: Turn, slots: int = 1) -> int:
    """剩余主请求槽位；任何请求（含摘要）存在未知费用时均禁止下一付费请求。"""

    attempts = turn.accounted_attempts
    if any(a.status != "running" and a.usage.completeness != "complete" for a in attempts) or any(
        c.unaccounted_request_possible for c in turn.compactions
    ):
        raise unknown_usage_stop(turn, "已有未知模型费用，禁止下一付费请求")
    return remaining_request_slots(turn, slots)


def remaining_request_slots(turn: Turn, slots: int = 1) -> int:
    """只按步数计算剩余主请求：主模型步骤及其重试共享max_steps，摘要请求不占槽位。"""

    main = turn.model_attempts
    used = turn.model_steps + len(main) - len({a.step for a in main})
    if turn.status == TurnStatus.CALLING_MODEL and not any(
        a.step == turn.model_steps for a in main
    ):
        used -= 1
    if used + slots > turn.budget.max_steps:
        raise KernelError("budget_exceeded", "主模型及重试尝试的总请求预算耗尽")
    return turn.budget.max_steps - used


def summary_requested(turn: Turn) -> bool:
    """下一主步骤之前是否已发起过摘要请求（含失败与未知费用的记录）。"""
    step = turn.model_steps + 1
    return any(record.plan.model_step == step for record in turn.compactions)


def require_summary_slot(turn: Turn, *, allow_unknown_usage: bool = False) -> None:
    """每个主步骤之前至多一次摘要请求，摘要后仍须留有该步骤的主请求槽位。

    摘要不占max_steps，单Turn付费请求因此至多为max_steps次主请求（含重试）加max_steps次摘要。
    """
    if allow_unknown_usage:
        require_reserved_unknown_usage(turn)
        remaining_request_slots(turn)
    else:
        require_request_slots(turn)
    if summary_requested(turn):
        raise KernelError("budget_exceeded", "本主步骤之前已发起摘要请求")


def summary_horizon(turn: Turn) -> int:
    """摘要的摊销跨度：摘要本身与其后至多两次主请求。"""
    return min(3, remaining_request_slots(turn) + 1)


def closing_reserve(turn: Turn) -> int:
    per_step = max(1, turn.budget.max_tokens // turn.budget.max_steps)
    return min(turn.budget.max_output_chars, per_step)


def pressure_history_allocation(turn: Turn, fixed_input: int) -> int:
    """剩余累计额度的重复输入目标；只是规划偏好，不是丢弃固定历史的授权。"""
    remaining = turn.budget.max_tokens - turn.usage.total_tokens
    horizon = summary_horizon(turn)
    return remaining // horizon - fixed_input - closing_reserve(turn)


def pressure_policy(
    turn: Turn, config: CompactionRuntimeConfig, fixed_input: int, history_input: int
) -> CompactionPolicy | None:
    horizon = summary_horizon(turn)
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


_SUMMARY_STAMP = (
    "[Summary through step {through}, written before step {before}; "
    "later messages are newer and override it.]\n"
)
_SUMMARY_STAMP_BEFORE_TURN = (
    "[Summary of earlier turns, written before step {before}; "
    "later messages are newer and override it.]\n"
)


def summary_stamp(through_step: int, before_step: int) -> str:
    """宿主写在摘要正文前的时点：摘要只代表其覆盖的较早历史，其后保留的对话更新。

    只用于触发式与被动压缩；预算压力压缩按精确预付的单段容量生成，不加时点。
    """
    template = _SUMMARY_STAMP if through_step > 0 else _SUMMARY_STAMP_BEFORE_TURN
    return template.format(through=through_step, before=before_step)


def summary_stamp_bytes(stamp: str) -> int:
    """时点在摘要JSON投影中占用的UTF-8字节（含转义），从摘要正文的预留中扣除。"""
    return len(json.dumps(stamp, ensure_ascii=False).encode()) - 2


def covered_through_step(
    events: Sequence[AgentEvent], turn_id: UUID, covered_item_ids: Iterable[UUID]
) -> int:
    """摘要覆盖的当前Turn Item最晚产生于哪个主步骤；Item只经ItemStarted进入Turn，没有则为0。"""
    step = 0
    produced: dict[UUID, int] = {}
    for event in events:
        if event.turn_id != turn_id:
            continue
        payload = event.payload
        if isinstance(payload, ModelAttemptStarted):
            step = payload.step
        elif isinstance(payload, ItemStarted) and step:
            produced.setdefault(payload.item_id, step)
    return max((produced[item] for item in covered_item_ids if item in produced), default=0)


def _summary_wrapper_bytes(compaction_id: UUID) -> int:
    text = SUMMARY_PROJECTION_HEADER + "x"
    wrapper = compaction_summary_item(CompactionSummary(compaction_id=compaction_id, text=text))
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
        "Keep exact identifiers, observed outcomes, failed approaches and uncertain effects; "
        "no to-do list. "
        "Omit copied source code, repeated logs and speculation; do not invent absent facts."
    )


def require_context_unchanged(before: PreparedContext, after: PreparedContext) -> None:
    """摘要期间只允许历史长度变化；指令、来源和权限事实必须重新求证。"""
    old, new = before.inspection.model_dump(), after.inspection.model_dump()
    fields = ("instruction_fingerprint", "limits", "fragments", "sources", "consistency")
    if any(old.get(field) != new.get(field) for field in fields):
        raise KernelError("context_compaction_source_changed", "摘要期间Context指令或来源发生漂移")
