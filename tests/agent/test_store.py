from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import RFC_4122

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    AgentEvent,
    Budget,
    EventDraft,
    ItemStarted,
    ThreadCreated,
    ToolResultContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


async def create(store: SQLiteSessionStore, workspace: Path):
    thread_id = new_id()
    draft = EventDraft(payload=ThreadCreated(workspace=str(workspace)))
    return await store.append(thread_id, [draft], expected_sequence=0), draft


def test_uuid7_layout() -> None:
    from time import time_ns

    before = time_ns() // 1_000_000
    values = [new_id() for _ in range(100)]
    after = time_ns() // 1_000_000
    assert len(set(values)) == 100
    assert all(v.version == 7 and v.variant == RFC_4122 for v in values)
    assert all(before <= v.int >> 80 <= after for v in values)


@pytest.mark.parametrize("point", ["session.after_events", "session.after_projection"])
async def test_event_projection_transaction_rolls_back(tmp_path: Path, point: str) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    thread, _ = await create(store, tmp_path)

    def fail(name: str) -> None:
        if name == point:
            raise RuntimeError("注入事务异常")

    failing = SQLiteSessionStore(store.path, fault=fail)
    with pytest.raises(RuntimeError, match="注入"):
        await failing.append(
            thread.thread_id,
            [
                EventDraft(
                    turn_id=new_id(),
                    payload=TurnStarted(
                        request_id="new", request_fingerprint="0" * 64, budget=Budget()
                    ),
                )
            ],
            expected_sequence=thread.sequence,
        )
    assert await store.get_thread(thread.thread_id) == thread
    assert len(await store.events(thread.thread_id)) == 1


async def test_invalid_result_and_partial_batch_are_atomic(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    thread, _ = await create(store, tmp_path)
    turn_id = new_id()
    drafts = [
        EventDraft(
            turn_id=turn_id,
            payload=TurnStarted(request_id="r", request_fingerprint="0" * 64, budget=Budget()),
        ),
        EventDraft(
            turn_id=turn_id,
            payload=ItemStarted(
                item_id=new_id(),
                content=ToolResultContent(call_id=new_id(), outcome="failed"),
            ),
        ),
    ]
    with pytest.raises(KernelError, match="乱序"):
        await store.append(thread.thread_id, drafts, expected_sequence=thread.sequence)
    assert await store.get_thread(thread.thread_id) == thread


async def test_replay_and_projection_rebuild(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "测试", request_id="r")
    snapshot = await store.get_thread(thread.thread_id)
    events = await store.events(thread.thread_id)
    assert replay(events) == snapshot
    assert replay(AgentEvent.model_validate_json(e.model_dump_json()) for e in events) == snapshot
    with sqlite3.connect(store.path) as database:
        database.execute("DELETE FROM agent_threads")
    with pytest.raises(KernelError, match="投影缺失"):
        await store.get_thread(thread.thread_id)
    assert await store.rebuild(thread.thread_id) == snapshot
    assert await store.get_thread(thread.thread_id) == snapshot
    with pytest.raises(KernelError):
        replay([*events[:2], events[3]])
    with pytest.raises(KernelError, match="重复"):
        replay([events[0], events[0]])


async def test_snapshot_tamper_detected_and_repaired(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    thread, _ = await create(store, tmp_path)
    with sqlite3.connect(store.path) as database:
        database.execute("UPDATE agent_threads SET snapshot_json = '{}'")
    with pytest.raises(KernelError, match="校验失败"):
        await store.get_thread(thread.thread_id)
    assert await store.rebuild(thread.thread_id) == thread


async def test_migration_idempotent_future_and_checksum(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    await asyncio.gather(store.initialize(), SQLiteSessionStore(store.path).initialize())
    assert store.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(store.path) as database:
        assert database.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == 6
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        database.execute("INSERT INTO agent_migrations VALUES (7, 'future')")
    with pytest.raises(KernelError, match="高于"):
        await store.initialize()
    with sqlite3.connect(store.path) as database:
        database.execute("DELETE FROM agent_migrations WHERE version = 7")
        database.execute("UPDATE agent_migrations SET checksum = 'changed'")
    with pytest.raises(KernelError, match="发生变化"):
        await store.initialize()


async def test_refuses_action_or_foreign_database(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE existing (value TEXT)")
        before = database.execute("PRAGMA journal_mode").fetchone()[0]
    with pytest.raises(KernelError, match="其他应用"):
        await SQLiteSessionStore(path).initialize()
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == before
        assert database.execute("SELECT name FROM sqlite_master").fetchall() == [("existing",)]


async def test_terminal_cannot_reopen(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "test", request_id="r")
    snapshot = await store.get_thread(thread.thread_id)
    with pytest.raises(KernelError):
        await store.append(
            thread.thread_id,
            [
                EventDraft(
                    turn_id=turn.turn_id,
                    payload=TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
                )
            ],
            expected_sequence=snapshot.sequence,
        )
    assert await store.get_thread(thread.thread_id) == snapshot


async def test_tool_result_pairing_and_order_in_reducer(tmp_path: Path) -> None:
    from harnessix.agent.models import (
        ItemFinished,
        ToolCallContent,
    )
    from harnessix.models.scripted import ScriptedProvider
    from tests.agent.helpers import RecordingTools, answer, tool_step

    store = SQLiteSessionStore(tmp_path / "sessions.db")
    provider = ScriptedProvider([tool_step("test.read", "test.read"), answer()])
    async with AgentRuntime(store, provider, RecordingTools()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    events = await store.events(thread.thread_id)
    execution_index = next(
        i
        for i, e in enumerate(events)
        if isinstance(e.payload, TurnStateChanged)
        and e.payload.status == TurnStatus.EXECUTING_TOOLS
    )
    prefix = events[: execution_index + 1]
    calls = [
        i.content for i in replay(prefix).turns[-1].items if isinstance(i.content, ToolCallContent)
    ]
    payload = ItemStarted(
        item_id=new_id(), content=ToolResultContent(call_id=calls[1].call_id, outcome="succeeded")
    )
    wrong_order = AgentEvent(
        thread_id=thread.thread_id,
        sequence=len(prefix) + 1,
        turn_id=turn.turn_id,
        payload=payload,
    )
    with pytest.raises(KernelError, match="乱序"):
        replay([*prefix, wrong_order])

    first_result = next(
        i
        for i, e in enumerate(events)
        if isinstance(e.payload, ItemFinished) and isinstance(e.payload.content, ToolResultContent)
    )
    settled_prefix = events[: first_result + 1]
    duplicate = AgentEvent(
        thread_id=thread.thread_id,
        sequence=len(settled_prefix) + 1,
        turn_id=turn.turn_id,
        payload=ItemStarted(
            item_id=new_id(),
            content=ToolResultContent(call_id=calls[0].call_id, outcome="succeeded"),
        ),
    )
    with pytest.raises(KernelError, match="乱序"):
        replay([*settled_prefix, duplicate])
