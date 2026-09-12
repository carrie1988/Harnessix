"""把Agent Protocol持久事件和临时Delta纯函数投影为产品视图。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from harnessix.product_ui.errors import ProductUIError
from harnessix.protocol.contracts import (
    EventsNextResult,
    EventsReplayResult,
    ItemPublicEvent,
    PublicBudget,
    PublicEvent,
    PublicFailure,
    PublicItem,
    PublicItemDelta,
    PublicUsage,
    ThreadView,
    TurnStartedPublicEvent,
    TurnStatePublicEvent,
    UsagePublicEvent,
)

MAX_RECENT_EVENT_CHECKPOINTS = 256


@dataclass(frozen=True, slots=True)
class ProjectedItem:
    """按首次出现位置稳定排序的持久Item。"""

    item: PublicItem
    turn_id: UUID
    first_cursor: int
    last_cursor: int
    final: bool


@dataclass(frozen=True, slots=True)
class TransientItemStream:
    """尚未由持久item_finished确认的流式文本。"""

    item_id: UUID
    turn_id: UUID
    model_step: int
    last_sequence: int
    text: str
    gap: bool = False


@dataclass(frozen=True, slots=True)
class TurnProjection:
    """由Thread Snapshot和增量Turn事件共同维护的当前Turn摘要。"""

    turn_id: UUID
    request_id: str | None
    status: str
    budget: PublicBudget | None
    usage: PublicUsage | None
    model_step: int
    error: PublicFailure | None = None


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    cursor: int
    event_id: UUID


@dataclass(frozen=True, slots=True)
class ProductViewState:
    """无I/O、可逐字段比较的单Thread产品投影视图。"""

    thread: ThreadView
    durable_cursor: int
    items: tuple[ProjectedItem, ...] = ()
    streams: tuple[TransientItemStream, ...] = ()
    current_turn: TurnProjection | None = None
    recent_events: tuple[ReplayCheckpoint, ...] = ()
    live_gap: bool = False

    def item_for(self, item_id: UUID) -> ProjectedItem | None:
        return next((entry for entry in self.items if entry.item.item_id == item_id), None)

    def stream_for(self, item_id: UUID) -> TransientItemStream | None:
        return next((entry for entry in self.streams if entry.item_id == item_id), None)


def _turn_from_snapshot(thread: ThreadView) -> TurnProjection | None:
    turn = thread.latest_turn
    if turn is None:
        return None
    return TurnProjection(
        turn_id=turn.turn_id,
        request_id=turn.request_id,
        status=turn.status,
        budget=turn.budget,
        usage=turn.usage,
        model_step=turn.model_steps,
        error=turn.error,
    )


def cold_product_view(thread: ThreadView) -> ProductViewState:
    """创建冷启动视图；Thread Snapshot的cursor不能替代从0重建Transcript。"""

    return ProductViewState(
        thread=thread,
        durable_cursor=0,
        current_turn=_turn_from_snapshot(thread),
    )


def refresh_thread_snapshot(state: ProductViewState, thread: ThreadView) -> ProductViewState:
    """暖重连刷新元数据，但保留同进程完整投影和已确认游标。"""

    if thread.thread_id != state.thread.thread_id:
        raise ProductUIError("projection_thread_mismatch", "Thread投影身份不匹配")
    if thread.cursor < state.durable_cursor:
        raise ProductUIError("projection_cursor_rollback", "Thread持久游标发生回退")
    return replace(state, thread=thread, current_turn=_turn_from_snapshot(thread))


def _replace_item(
    entries: tuple[ProjectedItem, ...],
    event: PublicEvent,
    data: ItemPublicEvent,
) -> tuple[ProjectedItem, ...]:
    if event.turn_id is None:
        raise ProductUIError("projection_event_invalid", "Item事件缺少Turn ID")
    if (data.type == "item_started") != (data.item.status == "started"):
        raise ProductUIError("projection_event_invalid", "Item事件类型与状态不一致")
    existing = next((entry for entry in entries if entry.item.item_id == data.item.item_id), None)
    final = data.type == "item_finished"
    if existing is not None and existing.final:
        raise ProductUIError("projection_item_conflict", "终态Item收到冲突的持久事件")
    projected = ProjectedItem(
        item=data.item,
        turn_id=event.turn_id,
        first_cursor=event.cursor if existing is None else existing.first_cursor,
        last_cursor=event.cursor,
        final=final,
    )
    updated = tuple(entry for entry in entries if entry.item.item_id != data.item.item_id)
    return tuple(
        sorted(
            (*updated, projected), key=lambda entry: (entry.first_cursor, str(entry.item.item_id))
        )
    )


def _apply_event(state: ProductViewState, event: PublicEvent) -> ProductViewState:
    data = event.data
    if isinstance(data, ItemPublicEvent):
        items = _replace_item(state.items, event, data)
        streams = state.streams
        if data.type == "item_finished":
            streams = tuple(entry for entry in streams if entry.item_id != data.item.item_id)
        return replace(state, items=items, streams=streams)
    if isinstance(data, TurnStartedPublicEvent):
        if event.turn_id is None:
            raise ProductUIError("projection_event_invalid", "Turn事件缺少Turn ID")
        return replace(
            state,
            current_turn=TurnProjection(
                turn_id=event.turn_id,
                request_id=data.request_id,
                status="accepted",
                budget=data.budget,
                usage=None,
                model_step=0,
            ),
        )
    if isinstance(data, TurnStatePublicEvent):
        if event.turn_id is None:
            raise ProductUIError("projection_event_invalid", "Turn事件缺少Turn ID")
        current = state.current_turn
        if current is None or current.turn_id != event.turn_id:
            raise ProductUIError("projection_turn_conflict", "Turn状态事件与当前Turn不匹配")
        return replace(state, current_turn=replace(current, status=data.status, error=data.error))
    if isinstance(data, UsagePublicEvent):
        if event.turn_id is None:
            raise ProductUIError("projection_event_invalid", "Usage事件缺少Turn ID")
        current = state.current_turn
        if current is None or current.turn_id != event.turn_id:
            raise ProductUIError("projection_turn_conflict", "Usage事件与当前Turn不匹配")
        if data.step < current.model_step:
            raise ProductUIError("projection_usage_rollback", "Usage步骤发生回退")
        return replace(
            state,
            current_turn=replace(current, usage=data.usage, model_step=data.step),
        )
    return state


def apply_replay_page(state: ProductViewState, page: EventsReplayResult) -> ProductViewState:
    """原子应用一页持久事实；重复页幂等，近期游标身份冲突失败关闭。"""

    if page.thread_id != state.thread.thread_id:
        raise ProductUIError("projection_thread_mismatch", "Replay与Thread投影身份不匹配")
    if page.scanned_through < state.durable_cursor:
        raise ProductUIError("projection_cursor_rollback", "Replay扫描游标发生回退")
    checkpoints = {entry.cursor: entry.event_id for entry in state.recent_events}
    updated = state
    new_checkpoints = list(state.recent_events)
    for event in page.events:
        if event.thread_id != page.thread_id:
            raise ProductUIError("projection_thread_mismatch", "Replay事件属于其他Thread")
        if event.cursor <= state.durable_cursor:
            known = checkpoints.get(event.cursor)
            if known is not None and known != event.event_id:
                raise ProductUIError("projection_event_conflict", "相同Replay游标对应不同事件")
            continue
        updated = _apply_event(updated, event)
        new_checkpoints.append(ReplayCheckpoint(cursor=event.cursor, event_id=event.event_id))
    return replace(
        updated,
        durable_cursor=page.scanned_through,
        recent_events=tuple(new_checkpoints[-MAX_RECENT_EVENT_CHECKPOINTS:]),
    )


def apply_item_delta(state: ProductViewState, delta: PublicItemDelta) -> ProductViewState:
    """严格按sequence追加临时文本；缺口后等待持久Item覆盖。"""

    if delta.thread_id != state.thread.thread_id:
        raise ProductUIError("projection_thread_mismatch", "Delta与Thread投影身份不匹配")
    persisted = state.item_for(delta.item_id)
    if persisted is not None and persisted.final:
        return state
    current = state.stream_for(delta.item_id)
    if current is not None:
        if current.turn_id != delta.turn_id or current.model_step != delta.model_step:
            raise ProductUIError("projection_delta_conflict", "Delta流身份发生变化")
        if delta.stream_sequence <= current.last_sequence or current.gap:
            return state
        if delta.stream_sequence != current.last_sequence + 1:
            stream = replace(current, gap=True)
        else:
            stream = replace(
                current,
                last_sequence=delta.stream_sequence,
                text=current.text + delta.delta,
            )
    else:
        stream = TransientItemStream(
            item_id=delta.item_id,
            turn_id=delta.turn_id,
            model_step=delta.model_step,
            last_sequence=delta.stream_sequence if delta.stream_sequence == 1 else 0,
            text=delta.delta if delta.stream_sequence == 1 else "",
            gap=delta.stream_sequence != 1,
        )
    streams = tuple(entry for entry in state.streams if entry.item_id != delta.item_id)
    return replace(
        state,
        streams=tuple(sorted((*streams, stream), key=lambda entry: str(entry.item_id))),
    )


def apply_events_next(state: ProductViewState, page: EventsNextResult) -> ProductViewState:
    """先应用权威Replay，再应用不推进游标的Live Delta。"""

    updated = apply_replay_page(state, page.replay)
    for delta in page.deltas:
        updated = apply_item_delta(updated, delta)
    return replace(updated, live_gap=updated.live_gap or page.live_gap)
