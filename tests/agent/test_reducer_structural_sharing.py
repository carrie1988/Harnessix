"""Reducer结构共享（ADR 0133）：追加事件不拷贝未变化的历史，取值与逐事件独立拷贝时相同。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.models import (
    AgentEvent,
    Budget,
    EventDraft,
    EventPayload,
    ItemStarted,
    TextContent,
    Thread,
    ThreadArchived,
    ThreadCreated,
    ToolCallContent,
    TurnStarted,
)
from harnessix.agent.reducer import apply_event, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer


def _event(
    thread_id: UUID, sequence: int, payload: EventPayload, turn_id: UUID | None = None
) -> AgentEvent:
    draft = EventDraft(turn_id=turn_id, payload=payload)
    return AgentEvent(**draft.model_dump(), thread_id=thread_id, sequence=sequence)


def _message(thread_id: UUID, sequence: int, turn_id: UUID, text: str) -> AgentEvent:
    content = TextContent(kind="user_message", text=text)
    return _event(thread_id, sequence, ItemStarted(item_id=uuid4(), content=content), turn_id)


def _thread_with_items(count: int) -> tuple[Thread, UUID]:
    thread_id, turn_id = uuid4(), uuid4()
    started = TurnStarted(request_id="fixture", request_fingerprint="1" * 64, budget=Budget())
    events = [
        _event(thread_id, 1, ThreadCreated(workspace="/workspace")),
        _event(thread_id, 2, started, turn_id),
        *(_message(thread_id, 3 + index, turn_id, f"fact {index}") for index in range(count)),
    ]
    return replay(events), turn_id


def test_new_item_leaves_earlier_items_shared_and_the_input_thread_unchanged() -> None:
    before, turn_id = _thread_with_items(3)
    frozen = before.model_copy(deep=True)
    event = _message(before.thread_id, before.sequence + 1, turn_id, "new fact")

    after = apply_event(before, event)

    assert before == frozen
    assert after.sequence == before.sequence + 1
    assert after.turns[0] is not before.turns[0]
    assert len(after.turns[0].items) == 4
    for old, new in zip(before.turns[0].items, after.turns[0].items, strict=False):
        assert new is old


def test_shared_result_equals_the_reduction_of_independent_copies() -> None:
    before, turn_id = _thread_with_items(3)
    event = _message(before.thread_id, before.sequence + 1, turn_id, "new fact")

    shared = apply_event(before, event)
    isolated = apply_event(before.model_copy(deep=True), event.model_copy(deep=True))

    assert shared == isolated
    assert shared.model_dump_json() == isolated.model_dump_json()


def test_archiving_shares_the_turns_of_the_archived_thread() -> None:
    thread_id = uuid4()
    before = replay([_event(thread_id, 1, ThreadCreated(workspace="/workspace"))])

    after = apply_event(before, _event(thread_id, 2, ThreadArchived(reason="done")))

    assert before.archive is None
    assert after.archive is not None and after.archive.reason == "done"
    assert after.turns is before.turns


async def test_appending_events_never_deep_copies_the_thread_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评测中每追加一个事件深拷贝一次全部历史，60步的会话里占宿主耗时的一成以上。"""
    store = SQLiteSessionStore(tmp_path / "state.db")
    await store.initialize()
    thread_id, turn_id = uuid4(), uuid4()
    await store.append(
        thread_id,
        [EventDraft(payload=ThreadCreated(workspace=str(tmp_path)))],
        expected_sequence=0,
    )

    def forbidden(self: Thread, memo: object = None) -> Thread:
        pytest.fail("追加事件不应深拷贝整个Thread")

    monkeypatch.setattr(Thread, "__deepcopy__", forbidden)
    started = TurnStarted(request_id="fixture", request_fingerprint="1" * 64, budget=Budget())
    await store.append(
        thread_id, [EventDraft(turn_id=turn_id, payload=started)], expected_sequence=1
    )
    for sequence in range(2, 6):
        content = TextContent(kind="user_message", text=f"fact {sequence}")
        draft = EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=uuid4(), content=content))
        appended = await store.append(thread_id, [draft], expected_sequence=sequence)
    monkeypatch.undo()

    assert appended.sequence == 6
    assert await store.get_thread(thread_id) == appended == replay(await store.events(thread_id))


async def test_stored_thread_does_not_alias_arguments_held_by_the_caller(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    await store.initialize()
    submitted: list[EventDraft] = []
    original_append = store.append

    async def recording_append(thread_id, drafts, *, expected_sequence):  # type: ignore[no-untyped-def]
        submitted.extend(drafts)
        return await original_append(thread_id, drafts, expected_sequence=expected_sequence)

    store.append = recording_append  # type: ignore[method-assign]
    call = ToolCallCompleted(call_id="call-0", tool="test.read", arguments={"paths": ["a.py"]})
    script = [
        [
            ResponseStarted(response_id="response"),
            call,
            ResponseCompleted(finish_reason="tool_calls"),
        ],
        answer(),
    ]
    async with AgentRuntime(store, ScriptedProvider(script), RecordingTools()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "read", request_id="read-turn")
    assert turn.status == "completed"
    stored = await store.get_thread(thread.thread_id)

    held = [
        draft.payload.content
        for draft in submitted
        if isinstance(draft.payload, ItemStarted)
        and isinstance(draft.payload.content, ToolCallContent)
    ]
    assert held
    for content in held:
        paths = content.arguments["paths"]
        assert isinstance(paths, list)
        paths.append("changed-by-caller.py")

    recorded = [
        item.content for item in stored.turns[0].items if isinstance(item.content, ToolCallContent)
    ]
    assert [content.arguments for content in recorded] == [{"paths": ["a.py"]}]
    assert await store.get_thread(thread.thread_id) == replay(await store.events(thread.thread_id))
