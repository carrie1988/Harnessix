from __future__ import annotations

from datetime import timedelta

from harnessix.agent.attempt_accounting import settle_observation
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    CompactionWindow,
    CompactionWindowActivated,
    Thread,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.usage import ModelAttempt
from harnessix.context.compaction import replay_compaction_candidate, replay_compaction_plan
from harnessix.context.compaction_ledger_contracts import (
    COMPACTION_OPEN,
    CompactionAttemptFinished,
    CompactionAttemptStarted,
    CompactionPlanned,
    CompactionRecord,
    CompactionRejected,
    CompactionSummarized,
    CompactionUsageObserved,
)
from harnessix.context.compaction_window import build_compaction_window


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KernelError("invalid_event", message)


def _before_deadline(turn: Turn, event: AgentEvent) -> None:
    _require(
        turn.created_at
        <= event.occurred_at
        < turn.created_at + timedelta(seconds=turn.budget.timeout_seconds),
        "摘要请求超过Turn时间边界",
    )


def _planned(thread: Thread, turn: Turn, event: AgentEvent, payload: CompactionPlanned) -> Turn:
    _require(turn.status == TurnStatus.PREPARING_CONTEXT, "压缩只能在准备阶段规划")
    _before_deadline(turn, event)
    _require(len(turn.compactions) < 1000, "压缩运行数量超过硬上限")
    _require(
        all(
            c.plan.compaction_id != payload.plan.compaction_id
            for t in thread.turns
            for c in t.compactions
        ),
        "压缩ID在Thread内重复",
    )
    _require(
        all(
            c.status not in COMPACTION_OPEN and c.plan.model_step != payload.plan.model_step
            for c in turn.compactions
        ),
        "已有开放压缩或目标步骤已规划",
    )
    _require(
        all(i.model_step != payload.plan.model_step for i in turn.model_history_inspections)
        and all(i.model_step != payload.plan.model_step for i in turn.context_inspections),
        "模型历史或Context冻结后不能开始压缩",
    )
    try:
        prepared = replay_compaction_plan(thread, payload.plan)
    except (KernelError, ValueError):
        raise KernelError("invalid_event", "压缩计划无法由Session事实验证") from None
    _require(prepared.model_history.new_decisions == payload.decisions, "压缩来源决定不一致")
    record = CompactionRecord(
        plan=payload.plan,
        status="planned",
        input_tokens_before=turn.usage.input_tokens,
        output_tokens_before=turn.usage.output_tokens,
        created_event_sequence=event.sequence,
        created_at=event.occurred_at,
    )
    return turn.model_copy(
        update={
            "compactions": (*turn.compactions, record),
            "tool_result_view_decisions": (*turn.tool_result_view_decisions, *payload.decisions),
        }
    )


def compaction_source(thread: Thread, turn: Turn, record: CompactionRecord) -> Thread:
    """还原计划验证边界；仅供已有开放事件守卫保护的账本候选校验使用。"""
    source_turn = turn.model_copy(
        update={
            "compactions": tuple(c for c in turn.compactions if c != record),
            "usage": Usage(
                input_tokens=record.input_tokens_before,
                output_tokens=record.output_tokens_before,
            ),
        }
    )
    return thread.model_copy(
        update={
            "sequence": record.plan.source_event_sequence,
            "turns": tuple(source_turn if t.turn_id == turn.turn_id else t for t in thread.turns),
        }
    )


def apply_compaction(thread: Thread, turn: Turn, event: AgentEvent) -> Turn:
    _require(event.occurred_at >= thread.updated_at, "摘要事件时间不可倒序")
    payload = event.payload
    if isinstance(payload, CompactionPlanned):
        return _planned(thread, turn, event, payload)
    assert isinstance(
        payload,
        CompactionAttemptStarted
        | CompactionUsageObserved
        | CompactionAttemptFinished
        | CompactionSummarized
        | CompactionRejected,
    )
    _require(
        turn.status in {TurnStatus.PREPARING_CONTEXT, TurnStatus.CANCELLING},
        "不在摘要结算状态",
    )
    record = next(
        (c for c in turn.compactions if c.plan.compaction_id == payload.compaction_id), None
    )
    _require(record is not None and record.status in COMPACTION_OPEN, "压缩未规划或已结束")
    assert record is not None
    _require(event.occurred_at >= record.created_at, "摘要事件早于计划")
    usage = turn.usage
    if isinstance(payload, CompactionAttemptStarted):
        _require(turn.status == TurnStatus.PREPARING_CONTEXT, "取消后不能发起摘要请求")
        _before_deadline(turn, event)
        _require(record.status == "planned", "摘要只允许单次请求")
        _require(turn.usage.total_tokens < turn.budget.max_tokens, "摘要已知Token预算耗尽")
        _require(
            payload.event.step == record.plan.model_step and payload.event.index == 1,
            "摘要请求与目标步骤或索引不一致",
        )
        _require(
            all(
                a.attempt_id != payload.event.attempt_id
                for t in thread.turns
                for a in t.accounted_attempts
            ),
            "尝试ID在Thread内重复",
        )
        _require(
            all(a.status != "running" for t in thread.turns for a in t.accounted_attempts),
            "存在未结算请求",
        )
        attempt = ModelAttempt(
            **payload.event.model_dump(exclude={"type"}), started_at=event.occurred_at
        )
        record = record.model_copy(update={"status": "sampling", "attempt": attempt})
    elif isinstance(payload, CompactionUsageObserved | CompactionAttemptFinished):
        _require(record.attempt is not None, "摘要请求尚未开始")
        assert record.attempt is not None
        _require(event.occurred_at >= record.attempt.started_at, "摘要观测早于请求")
        attempt, inputs, outputs = settle_observation(
            record.attempt, payload.event, event.occurred_at
        )
        usage = Usage(
            input_tokens=usage.input_tokens + inputs, output_tokens=usage.output_tokens + outputs
        )
        record = record.model_copy(update={"attempt": attempt})
    elif isinstance(payload, CompactionSummarized):
        _require(turn.status == TurnStatus.PREPARING_CONTEXT, "取消后不能提交摘要候选")
        _before_deadline(turn, event)
        _require(
            record.attempt is not None and record.attempt.status == "completed",
            "候选需要已结算的成功请求",
        )
        try:
            candidate = replay_compaction_candidate(
                compaction_source(thread, turn, record), record.plan, payload.summary
            )
        except (KernelError, ValueError):
            raise KernelError("invalid_event", "摘要候选无法由Session事实验证") from None
        _require(
            candidate.history_sha256 == payload.candidate_history_sha256
            and candidate.history_tokens == payload.candidate_history_tokens,
            "摘要候选指纹或Token计数不一致",
        )
        record = record.model_copy(
            update={
                "status": "summarized",
                "summary": payload.summary,
                "candidate_history_sha256": payload.candidate_history_sha256,
                "candidate_history_tokens": payload.candidate_history_tokens,
                "finished_event_sequence": event.sequence,
                "finished_at": event.occurred_at,
            }
        )
    else:
        _require(
            record.attempt is None or record.attempt.status != "running",
            "拒绝候选前必须结算请求",
        )
        record = record.model_copy(
            update={
                "status": payload.outcome,
                "failure": payload.failure,
                "unaccounted_request_possible": payload.unaccounted_request_possible,
                "finished_event_sequence": event.sequence,
                "finished_at": event.occurred_at,
            }
        )
    try:
        record = CompactionRecord.model_validate(record.model_dump())
    except ValueError:
        raise KernelError("invalid_event", "摘要记录与时间或阶段不一致") from None
    return turn.model_copy(
        update={
            "usage": usage,
            "compactions": tuple(
                record if c.plan.compaction_id == record.plan.compaction_id else c
                for c in turn.compactions
            ),
        }
    )


def activate_compaction_window(
    thread: Thread,
    turn: Turn,
    event: AgentEvent,
    payload: CompactionWindowActivated,
) -> CompactionWindow:
    _require(turn.status == TurnStatus.PREPARING_CONTEXT, "活动窗口只能在准备阶段发布")
    _require(
        all(i.model_step != payload.window.model_step for i in turn.model_history_inspections)
        and all(i.model_step != payload.window.model_step for i in turn.context_inspections),
        "模型历史或Context冻结后不能发布窗口",
    )
    record = next(
        (
            record
            for record in turn.compactions
            if record.plan.compaction_id == payload.window.compaction_id
        ),
        None,
    )
    _require(
        record is not None
        and record.status == "summarized"
        and record.summary is not None
        and record.finished_event_sequence == thread.sequence,
        "窗口候选不是当前最新已结算摘要",
    )
    assert record is not None and record.summary is not None
    _require(
        all(window.window_id != payload.window.window_id for window in thread.compaction_windows),
        "窗口ID在Thread内重复",
    )
    try:
        candidate = replay_compaction_candidate(
            compaction_source(thread, turn, record), record.plan, record.summary
        )
        expected = build_compaction_window(
            thread,
            record,
            candidate,
            window_id=payload.window.window_id,
            activated_event_sequence=event.sequence,
            activated_at=event.occurred_at,
        )
    except (KernelError, ValueError):
        raise KernelError("invalid_event", "活动窗口无法由候选与Session事实验证") from None
    _require(payload.window == expected, "活动窗口内容与Session事实不一致")
    return expected
