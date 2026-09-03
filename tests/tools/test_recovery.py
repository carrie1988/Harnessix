from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


@pytest.mark.parametrize(
    "point,count",
    [("runtime.before_tool", 0), ("runtime.after_tool", 1), ("runtime.before_terminal", 1)],
)
@pytest.mark.parametrize("tool", ["read_file", "glob", "grep"])
async def test_real_read_process_crash_does_not_repeat(tmp_path, point, count, tool):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("读取夹具")
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(root))
    counter = tmp_path / "count"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.tools.crash_worker",
        str(store.path),
        str(thread.thread_id),
        str(root),
        point,
        str(counter),
        tool,
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), 20) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert (int(counter.read_text()) if counter.exists() else 0) == count
    provider = FakeProvider()
    reads = []

    class RecoveredTools(CodingToolRuntime):
        async def execute(self, call, cancel):
            reads.append(call)
            return await super().execute(call, cancel)

    async with RecoveredTools(root) as tools:
        async with AgentRuntime(store, provider, tools) as runtime:
            turn = (await store.get_thread(thread.thread_id)).turns[-1]
            assert turn.status == TurnStatus.INTERRUPTED
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == turn
            assert provider.requests == []
            assert reads == []
            assert (int(counter.read_text()) if counter.exists() else 0) == count
            assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                thread.thread_id
            )
