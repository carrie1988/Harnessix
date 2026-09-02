from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import ItemStatus, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_semantic_items import prepared


@pytest.mark.parametrize("kind", ["plan", "context_compaction", "error"])
@pytest.mark.parametrize(
    "point", ["session.after_events", "session.after_projection", "session.after_commit"]
)
async def test_semantic_commit_crash_matrix(tmp_path: Path, kind: str, point: str) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepared(store, tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.agent.semantic_crash_worker",
        str(store.path),
        str(thread.thread_id),
        kind,
        point,
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), 10) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    provider = FakeProvider()
    async with AgentRuntime(store, provider):
        turn = (await store.get_thread(thread.thread_id)).turns[-1]
        committed = point == "session.after_commit"
        assert turn.status == (
            TurnStatus.FAILED if kind == "error" and committed else TurnStatus.INTERRUPTED
        )
        if kind != "error":
            assert sum(i.content.kind == kind for i in turn.items) == int(committed)
        else:
            assert turn.error.code == ("budget_exceeded" if committed else "process_interrupted")
        assert all(i.status != ItemStatus.STARTED for i in turn.items)
        assert provider.requests == []
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )
