"""状态栏的Token与费用来自Turn累计用量：逐步用量事件累加，快照已含的步骤不重复计入。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from harnessix.product_ui import apply_replay_page, cold_product_view, refresh_thread_snapshot
from harnessix.protocol.contracts import (
    EventsReplayResult,
    PublicBudget,
    PublicEvent,
    PublicUsage,
    ThreadView,
    TurnStartedPublicEvent,
    TurnView,
    UsagePublicEvent,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
BUDGET = PublicBudget(
    max_steps=50,
    max_tokens=1_000_000,
    timeout_seconds=600.0,
    max_output_chars=100_000,
    max_tool_calls_per_step=8,
)


def _usage(input_tokens: int, output_tokens: int) -> PublicUsage:
    return PublicUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _thread(thread_id: UUID, *, cursor: int, turn: TurnView | None = None) -> ThreadView:
    return ThreadView(
        thread_id=thread_id,
        workspace="/workspace",
        cursor=cursor,
        turn_count=0 if turn is None else 1,
        latest_turn=turn,
        created_at=NOW,
        updated_at=NOW,
    )


def _event(thread_id: UUID, turn_id: UUID, cursor: int, data: object) -> PublicEvent:
    return PublicEvent.model_validate(
        {
            "event_id": uuid4(),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "cursor": cursor,
            "occurred_at": NOW,
            "data": data,
        }
    )


def _page(thread_id: UUID, *events: PublicEvent) -> EventsReplayResult:
    return EventsReplayResult(
        thread_id=thread_id,
        events=events,
        scanned_through=max(event.cursor for event in events),
        has_more=False,
    )


def test_step_usage_events_add_up_instead_of_replacing_the_turn_total() -> None:
    thread_id, turn_id = uuid4(), uuid4()
    started = TurnStartedPublicEvent(request_id="r1", budget=BUDGET)
    view = apply_replay_page(
        cold_product_view(_thread(thread_id, cursor=0)),
        _page(
            thread_id,
            _event(thread_id, turn_id, 1, started),
            _event(thread_id, turn_id, 2, UsagePublicEvent(step=1, usage=_usage(8000, 300))),
            _event(thread_id, turn_id, 5, UsagePublicEvent(step=2, usage=_usage(9000, 200))),
        ),
    )
    assert view.current_turn is not None
    # 此前第二步的用量覆盖了第一步，状态栏数字随步骤忽大忽小。
    assert view.current_turn.usage == _usage(17000, 500)


def test_a_snapshot_refresh_does_not_count_its_steps_twice() -> None:
    thread_id, turn_id = uuid4(), uuid4()
    started = TurnStartedPublicEvent(request_id="r1", budget=BUDGET)
    step_one = _event(thread_id, turn_id, 2, UsagePublicEvent(step=1, usage=_usage(8000, 300)))
    view = apply_replay_page(
        cold_product_view(_thread(thread_id, cursor=0)),
        _page(thread_id, _event(thread_id, turn_id, 1, started), step_one),
    )
    snapshot_turn = TurnView(
        turn_id=turn_id,
        request_id="r1",
        status="calling_model",
        budget=BUDGET,
        usage=_usage(17000, 500),
        model_steps=2,
        created_at=NOW,
    )
    # 快照游标5已含两步用量；随后重放的第二步事件（游标5）不能再加一次。
    view = refresh_thread_snapshot(view, _thread(thread_id, cursor=5, turn=snapshot_turn))
    step_two = _event(thread_id, turn_id, 5, UsagePublicEvent(step=2, usage=_usage(9000, 200)))
    step_three = _event(thread_id, turn_id, 7, UsagePublicEvent(step=3, usage=_usage(1000, 100)))
    view = apply_replay_page(view, _page(thread_id, step_two, step_three))
    assert view.current_turn is not None
    assert view.current_turn.usage == _usage(18000, 600)
    assert view.current_turn.model_step == 3
