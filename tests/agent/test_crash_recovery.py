from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    EventDraft,
    ItemStatus,
    ThreadCreated,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import pending_calls
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


@pytest.mark.parametrize(
    ("point", "turns", "count"),
    [
        ("session.after_events", 0, 0),
        ("session.after_projection", 0, 0),
        ("runtime.after_turn_started", 1, 0),
        ("runtime.after_tool_call", 1, 0),
        ("runtime.before_tool", 1, 0),
        ("runtime.after_tool", 1, 1),
        ("runtime.before_terminal", 1, 1),
    ],
)
async def test_process_crash_recovers_without_replaying_tool(
    tmp_path: Path, point: str, turns: int, count: int
) -> None:
    database = tmp_path / "session.db"
    counter = tmp_path / "count"
    store = SQLiteSessionStore(database)
    await store.initialize()
    thread = await store.append(
        new_id(),
        [EventDraft(payload=ThreadCreated(workspace=str(tmp_path)))],
        expected_sequence=0,
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.agent.crash_worker",
        str(database),
        str(thread.thread_id),
        point,
        str(counter),
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), timeout=10) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    async with AgentRuntime(SQLiteSessionStore(database), FakeProvider()) as runtime:
        recovered = await runtime.store.get_thread(thread.thread_id)
        assert len(recovered.turns) == turns
        if recovered.turns:
            turn = recovered.turns[-1]
            assert turn.status == TurnStatus.INTERRUPTED
            assert not pending_calls(turn)
            assert all(item.status != ItemStatus.STARTED for item in turn.items)
            if point in {"runtime.after_tool_call", "runtime.before_tool", "runtime.after_tool"}:
                results = [
                    item.content
                    for item in turn.items
                    if isinstance(item.content, ToolResultContent)
                ]
                assert len(results) == 1
                assert results[0].outcome == "failed"

    assert (int(counter.read_text()) if counter.exists() else 0) == count
