"""持久Agent状态机：将有序Agent事件确定性投影为Session状态，不执行外部副作用。"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from harnessix.agent.compaction_reducer import activate_compaction_window, apply_compaction
from harnessix.agent.errors import KernelError
from harnessix.agent.item_reducer import _finish_item, _start_item
from harnessix.agent.models import (
    TERMINAL_TURNS,
    AgentEvent,
    CompactionWindowActivated,
    ItemFinished,
    ItemStarted,
    ModelHistoryPrepared,
    Thread,
    ThreadArchived,
    ThreadArchiveRecord,
    ThreadCreated,
    ThreadForked,
    ToolResultContent,
    Turn,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.reducer_support import get_turn as get_turn
from harnessix.agent.reducer_support import pending_calls as pending_calls
from harnessix.agent.reducer_support import require as require
from harnessix.agent.turn_reducer import (
    _change_state,
    _model_attempt,
    _prepare_context,
    _prepare_model_history,
)
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
)
from harnessix.context.compaction_ledger_contracts import COMPACTION_OPEN, CompactionEvent
from harnessix.context.contracts import ContextPrepared
from harnessix.context.tool_result_view import history_items


def apply_event(thread: Thread | None, event: AgentEvent) -> Thread:
    """唯一的状态投影器；在线提交和离线 Replay 使用相同校验。"""
    payload = event.payload
    if thread is None:
        require(event.sequence == 1, "首事件 sequence 必须为 1")
        require(isinstance(payload, ThreadCreated | ThreadForked), "首事件必须创建或Fork Thread")
        require(event.turn_id is None, "Thread 事件不能绑定 Turn")
        assert isinstance(payload, ThreadCreated | ThreadForked)
        return Thread(
            thread_id=event.thread_id,
            workspace=payload.workspace,
            sequence=1,
            fork_snapshot=payload.snapshot if isinstance(payload, ThreadForked) else None,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
    require(thread.thread_id == event.thread_id, "事件属于其他 Thread")
    require(event.sequence == thread.sequence + 1, "事件序号存在缺口或倒序")
    require(not isinstance(payload, ThreadCreated | ThreadForked), "Thread 不可重复创建或Fork")
    if isinstance(payload, ThreadArchived):
        require(event.turn_id is None, "归档事件不能绑定Turn")
        require(thread.archive is None, "Thread已经归档")
        require(thread.active_turn_id is None, "活跃Turn结束前不能归档Thread")
        return thread.model_copy(
            update={
                "archive": ThreadArchiveRecord(
                    archived_event_sequence=event.sequence,
                    archived_at=event.occurred_at,
                    reason=payload.reason,
                ),
                "sequence": event.sequence,
                "updated_at": event.occurred_at,
            },
            deep=True,
        )
    require(thread.archive is None, "归档Thread不可修改")
    require(event.turn_id is not None, "Turn 事件缺少 turn_id")
    assert event.turn_id is not None
    thread_updates: dict[str, object] = {}
    if isinstance(payload, TurnStarted):
        require(thread.active_turn_id is None, "同一 Thread 已存在活跃 Turn")
        require(all(t.turn_id != event.turn_id for t in thread.turns), "Turn ID 已存在")
        require(
            all(t.request_id != payload.request_id for t in thread.turns),
            "request_id 已绑定其他 Turn",
        )
        if payload.retry_of_turn_id is not None:
            require(event.schema_version >= 17, "Turn Retry来源需要Agent Event v17")
            require(bool(thread.turns), "Turn Retry来源不存在")
            source = thread.turns[-1]
            require(source.turn_id == payload.retry_of_turn_id, "只能重试最新Turn")
            require(
                source.status in {TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED},
                "Turn Retry来源状态不可重试",
            )
            require(
                not any(
                    isinstance(item.content, ToolResultContent)
                    and item.content.outcome == "unknown"
                    for item in source.items
                ),
                "存在未知工具效果，禁止Turn Retry",
            )
        turn = Turn(
            turn_id=event.turn_id,
            request_id=payload.request_id,
            request_fingerprint=payload.request_fingerprint,
            retry_of_turn_id=payload.retry_of_turn_id,
            execution_mode=payload.execution_mode,
            budget=payload.budget,
            trace_context=payload.trace_context,
            created_at=event.occurred_at,
        )
        turns = (*thread.turns, turn)
    else:
        require(thread.active_turn_id == event.turn_id, "不能修改非活跃 Turn")
        turn = get_turn(thread, event.turn_id)
        require(turn.status not in TERMINAL_TURNS, "终态不可重开")
        if any(c.status in COMPACTION_OPEN for c in turn.compactions):
            require(
                isinstance(payload, CompactionEvent)
                or (
                    isinstance(payload, TurnStateChanged)
                    and payload.status == TurnStatus.CANCELLING
                ),
                "开放压缩期间只能推进摘要账本或取消",
            )
        if isinstance(payload, TurnStateChanged):
            turn = _change_state(thread, turn, event, payload)
        elif isinstance(payload, ItemStarted):
            require(
                all(item.item_id != payload.item_id for item in history_items(thread))
                and all(
                    item.item_id != payload.item_id
                    for turn_in_thread in thread.turns
                    for item in turn_in_thread.items
                ),
                "Item ID 在 Thread 内重复",
            )
            turn = _start_item(thread, turn, event, payload)
        elif isinstance(payload, ItemFinished):
            turn = _finish_item(turn, event, payload)
        elif isinstance(payload, ModelAttemptStarted | ModelUsageObserved | ModelAttemptFinished):
            if isinstance(payload, ModelAttemptStarted):
                require(
                    all(
                        a.attempt_id != payload.attempt_id
                        for t in thread.turns
                        for a in t.accounted_attempts
                    ),
                    "尝试 ID 在 Thread 内重复",
                )
            turn = _model_attempt(turn, event)
        elif isinstance(payload, CompactionEvent):
            turn = apply_compaction(thread, turn, event)
        elif isinstance(payload, CompactionWindowActivated):
            window = activate_compaction_window(thread, turn, event, payload)
            thread_updates = {
                "compaction_windows": (*thread.compaction_windows, window),
                "active_compaction_window_id": window.window_id,
            }
        elif isinstance(payload, ContextPrepared):
            turn = _prepare_context(turn, payload)
        elif isinstance(payload, ModelHistoryPrepared):
            turn = _prepare_model_history(thread, turn, payload)
        elif isinstance(payload, UsageRecorded):
            require(turn.status == TurnStatus.CALLING_MODEL, "用量只能在模型步骤内记录")
            require(payload.step == turn.model_steps, "用量不属于当前模型步骤")
            require(payload.step > turn.usage_step, "用量重复记账")
            attempts = [a for a in turn.model_attempts if a.step == payload.step]
            if attempts:
                last = attempts[-1]
                require(last.status == "completed", "模型响应完成前必须结算成功尝试")
                require(
                    payload.usage.input_tokens == last.usage.input_tokens
                    and payload.usage.output_tokens == last.usage.output_tokens,
                    "响应用量与尝试事实不一致",
                )
            turn = turn.model_copy(
                update={
                    "usage_step": payload.step,
                    "usage": turn.usage
                    if attempts
                    else Usage(
                        input_tokens=turn.usage.input_tokens + payload.usage.input_tokens,
                        output_tokens=turn.usage.output_tokens + payload.usage.output_tokens,
                    ),
                }
            )
        turns = tuple(turn if t.turn_id == turn.turn_id else t for t in thread.turns)
    return thread.model_copy(
        update={
            "turns": turns,
            "active_turn_id": None if turn.status in TERMINAL_TURNS else turn.turn_id,
            "sequence": event.sequence,
            "updated_at": event.occurred_at,
            **thread_updates,
        },
        deep=True,
    )


def replay(events: Iterable[AgentEvent]) -> Thread:
    """按序重放完整Transcript；重复事件、序号缺口和空历史均拒绝恢复。"""
    thread: Thread | None = None
    seen: set[UUID] = set()
    for event in events:
        require(event.event_id not in seen, "重放事件 ID 重复")
        seen.add(event.event_id)
        thread = apply_event(thread, event)
    if thread is None:
        raise KernelError("empty_transcript", "Transcript 为空")
    return thread
