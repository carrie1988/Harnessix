"""拒绝前缀、已闭合反馈及原Turn期限的取消/崩溃恢复回归。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import Budget, ToolCallRejectionContent, ToolResultContent, TurnStatus
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_rejections import require_closed_rejections
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallRejected
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("stop", ["user", "task", "timeout"])
async def test_original_cancel_and_timeout_never_reexecute_rejected_call(tmp_path, closed, stop):
    waiting, released = asyncio.Event(), asyncio.Event()
    requests = []

    class Provider:
        async def stream(self, request, token):
            requests.append(request)
            try:
                yield ResponseStarted(response_id="r")
                if request.step == 1:
                    yield ToolCallRejected(call_id="rejected", argument_chars=2)
                    if closed:
                        yield ResponseCompleted(finish_reason="tool_calls")
                        return
                waiting.set()
                await asyncio.Event().wait()
            finally:
                released.set()

    store = SQLiteSessionStore(tmp_path / "stop.db")
    tools = RecordingTools()
    async with AgentRuntime(store, Provider(), tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(
            runtime.run_turn(
                thread.thread_id,
                "取消拒绝纠正",
                request_id="stop",
                budget=Budget(timeout_seconds=2 if stop == "timeout" else 30),
            )
        )
        await asyncio.wait_for(waiting.wait(), 3)
        if stop == "user":
            current = await store.get_thread(thread.thread_id)
            await runtime.cancel(thread.thread_id, current.active_turn_id)
        elif stop == "task":
            task.cancel()
        if stop == "task":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            await asyncio.wait_for(task, 3)
    assert released.is_set() and tools.calls == []
    snapshot = await store.get_thread(thread.thread_id)
    require_closed_rejections(snapshot)
    turn = snapshot.turns[-1]
    assert turn.status == (TurnStatus.FAILED if stop == "timeout" else TurnStatus.CANCELLED)
    if stop == "timeout":
        assert turn.error.code == "time_budget_exceeded"
    assert not pending_calls(turn)
    assert len(requests) == (2 if closed else 1)
    assert len([i for i in turn.items if isinstance(i.content, ToolCallRejectionContent)]) == int(
        closed
    )
    provider = FakeProvider()
    async with AgentRuntime(SQLiteSessionStore(store.path), provider) as reopened:
        assert await reopened.store.get_thread(thread.thread_id) == snapshot
    assert provider.requests == [] and replay(await store.events(thread.thread_id)) == snapshot


@pytest.mark.parametrize(
    "point,committed",
    [
        ("before-terminal", False),
        ("session.after_events", False),
        ("session.after_projection", False),
        ("session.after_commit", True),
        ("runtime.after_tool_call", True),
    ],
)
async def test_hard_exit_commits_whole_rejection_pair_or_nothing(tmp_path, point, committed):
    path = tmp_path / "crash.db"
    async with AgentRuntime(SQLiteSessionStore(path), FakeProvider()) as runtime:
        source = await runtime.create_thread(str(tmp_path))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.agent.rejection_crash_worker",
        str(path),
        str(source.thread_id),
        point,
        cwd=Path(__file__).parents[2],
        env=env,
    )
    try:
        assert await asyncio.wait_for(child.wait(), 10) == 77
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    provider, tools = FakeProvider(), RecordingTools()
    store = SQLiteSessionStore(path)
    async with AgentRuntime(store, provider, tools):
        recovered = await store.get_thread(source.thread_id)
    turn = recovered.turns[-1]
    assert turn.status == TurnStatus.INTERRUPTED and not pending_calls(turn)
    require_closed_rejections(recovered)
    rejects = [i for i in turn.items if isinstance(i.content, ToolCallRejectionContent)]
    results = [i for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert len(rejects) == len(results) == int(committed)
    assert provider.requests == [] and tools.calls == []
    assert replay(await store.events(source.thread_id)) == recovered
