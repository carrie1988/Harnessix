from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from harnessix.product_ui import (
    ProductUIError,
    apply_item_delta,
    apply_replay_page,
    cold_product_view,
)
from harnessix.protocol.contracts import (
    EventsReplayResult,
    ItemPublicEvent,
    PublicBudget,
    PublicEvent,
    PublicItem,
    PublicItemDelta,
    PublicTextContent,
    ThreadView,
    TurnStartedPublicEvent,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _thread(thread_id: UUID, *, cursor: int = 20) -> ThreadView:
    return ThreadView(
        thread_id=thread_id,
        workspace="/workspace",
        cursor=cursor,
        turn_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _item(item_id: UUID, text: str, *, status: str = "completed") -> PublicItem:
    return PublicItem(
        item_id=item_id,
        status=status,
        content=PublicTextContent(kind="assistant_message", text=text),
    )


def _item_event(
    thread_id: UUID,
    item_id: UUID,
    *,
    cursor: int,
    event_id: UUID | None = None,
    text: str = "完成",
    event_type: str = "item_finished",
) -> PublicEvent:
    return PublicEvent(
        event_id=event_id or uuid4(),
        thread_id=thread_id,
        turn_id=uuid4(),
        cursor=cursor,
        occurred_at=NOW,
        data=ItemPublicEvent(type=event_type, item=_item(item_id, text)),
    )


def _page(thread_id: UUID, *events: PublicEvent, scanned_through: int) -> EventsReplayResult:
    return EventsReplayResult(
        thread_id=thread_id,
        events=events,
        scanned_through=scanned_through,
        has_more=False,
    )


def test_cold_projection_starts_at_zero_and_is_deterministic() -> None:
    thread_id = uuid4()
    item_id = uuid4()
    event = _item_event(thread_id, item_id, cursor=7)
    page = _page(thread_id, event, scanned_through=9)

    initial = cold_product_view(_thread(thread_id, cursor=20))
    first = apply_replay_page(initial, page)
    second = apply_replay_page(cold_product_view(_thread(thread_id, cursor=20)), page)

    assert initial.durable_cursor == 0
    assert first == second
    assert first.durable_cursor == 9
    assert first.item_for(item_id).item.content.text == "完成"  # type: ignore[union-attr]


def test_duplicate_replay_is_idempotent_and_recent_conflict_fails() -> None:
    thread_id = uuid4()
    item_id = uuid4()
    event_id = uuid4()
    event = _item_event(thread_id, item_id, cursor=3, event_id=event_id)
    page = _page(thread_id, event, scanned_through=3)
    applied = apply_replay_page(cold_product_view(_thread(thread_id)), page)

    assert apply_replay_page(applied, page) == applied

    conflict = _item_event(thread_id, item_id, cursor=3, event_id=uuid4())
    with pytest.raises(ProductUIError) as error:
        apply_replay_page(applied, _page(thread_id, conflict, scanned_through=3))
    assert error.value.code == "projection_event_conflict"


def test_replay_rejects_cross_thread_and_cursor_rollback() -> None:
    thread_id = uuid4()
    state = apply_replay_page(
        cold_product_view(_thread(thread_id)),
        _page(thread_id, scanned_through=8),
    )

    with pytest.raises(ProductUIError) as cross_thread:
        apply_replay_page(state, _page(uuid4(), scanned_through=8))
    assert cross_thread.value.code == "projection_thread_mismatch"

    with pytest.raises(ProductUIError) as rollback:
        apply_replay_page(state, _page(thread_id, scanned_through=7))
    assert rollback.value.code == "projection_cursor_rollback"


def test_turn_started_projects_the_durable_accepted_state() -> None:
    thread_id = uuid4()
    turn_id = uuid4()
    event = PublicEvent(
        event_id=uuid4(),
        thread_id=thread_id,
        turn_id=turn_id,
        cursor=1,
        occurred_at=NOW,
        data=TurnStartedPublicEvent(
            request_id="request-1",
            budget=PublicBudget(
                max_steps=4,
                max_tokens=1024,
                timeout_seconds=60.0,
                max_output_chars=4096,
                max_tool_calls_per_step=8,
            ),
        ),
    )

    projected = apply_replay_page(
        cold_product_view(_thread(thread_id)),
        _page(thread_id, event, scanned_through=1),
    )

    assert projected.current_turn is not None
    assert projected.current_turn.turn_id == turn_id
    assert projected.current_turn.status == "accepted"


@pytest.mark.parametrize(
    ("event_type", "item_status"),
    (("item_started", "completed"), ("item_finished", "started")),
)
def test_replay_rejects_item_event_status_mismatch(
    event_type: str,
    item_status: str,
) -> None:
    thread_id = uuid4()
    event = _item_event(
        thread_id,
        uuid4(),
        cursor=1,
        event_type=event_type,
    ).model_copy(
        update={
            "data": ItemPublicEvent(
                type=event_type,
                item=_item(uuid4(), "invalid", status=item_status),
            )
        }
    )

    with pytest.raises(ProductUIError) as error:
        apply_replay_page(
            cold_product_view(_thread(thread_id)),
            _page(thread_id, event, scanned_through=1),
        )

    assert error.value.code == "projection_event_invalid"


def test_replay_rejects_item_event_without_turn_identity() -> None:
    thread_id = uuid4()
    event = _item_event(thread_id, uuid4(), cursor=1).model_copy(update={"turn_id": None})

    with pytest.raises(ProductUIError) as error:
        apply_replay_page(
            cold_product_view(_thread(thread_id)),
            _page(thread_id, event, scanned_through=1),
        )

    assert error.value.code == "projection_event_invalid"


def test_delta_sequence_gap_and_final_event_authority() -> None:
    thread_id = uuid4()
    turn_id = uuid4()
    item_id = uuid4()
    state = cold_product_view(_thread(thread_id))

    state = apply_item_delta(
        state,
        PublicItemDelta(
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            model_step=1,
            stream_sequence=1,
            delta="Hel",
        ),
    )
    duplicate = apply_item_delta(
        state,
        PublicItemDelta(
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            model_step=1,
            stream_sequence=1,
            delta="ignored",
        ),
    )
    assert duplicate == state

    gap = apply_item_delta(
        state,
        PublicItemDelta(
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            model_step=1,
            stream_sequence=3,
            delta="lo",
        ),
    )
    assert gap.stream_for(item_id).text == "Hel"  # type: ignore[union-attr]
    assert gap.stream_for(item_id).gap  # type: ignore[union-attr]

    final = _item_event(thread_id, item_id, cursor=5, text="Hello")
    completed = apply_replay_page(gap, _page(thread_id, final, scanned_through=5))
    assert completed.stream_for(item_id) is None
    assert completed.item_for(item_id).item.content.text == "Hello"  # type: ignore[union-attr]

    ignored = apply_item_delta(
        completed,
        PublicItemDelta(
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            model_step=1,
            stream_sequence=4,
            delta="ignored",
        ),
    )
    assert ignored == completed


def test_delta_rejects_cross_thread_and_identity_change() -> None:
    thread_id = uuid4()
    turn_id = uuid4()
    item_id = uuid4()
    state = cold_product_view(_thread(thread_id))
    first = PublicItemDelta(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=item_id,
        model_step=1,
        stream_sequence=1,
        delta="a",
    )
    state = apply_item_delta(state, first)

    with pytest.raises(ProductUIError) as mismatch:
        apply_item_delta(state, first.model_copy(update={"thread_id": uuid4()}))
    assert mismatch.value.code == "projection_thread_mismatch"

    with pytest.raises(ProductUIError) as identity:
        apply_item_delta(state, first.model_copy(update={"turn_id": uuid4(), "stream_sequence": 2}))
    assert identity.value.code == "projection_delta_conflict"
