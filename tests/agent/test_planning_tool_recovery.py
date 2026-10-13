"""计划提交锁内停止检查与真实进程崩溃；仅使用本地SQLite及ScriptedProvider。"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent import approvals
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    EventDraft,
    ItemStarted,
    ItemStatus,
    PlanContent,
    ToolCallContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.planning import execute_plan_update
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_planning_tool import plan_call, plan_items, proposal_step, result_for


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_stop_at_before_commit_fault_is_rechecked_inside_thread_lock(
    tmp_path, monkeypatch, stop
):
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([proposal_step(plan_call()), answer()])
    faults: list[str] = []

    def fault(point):
        if point != "runtime.before_plan_update_commit":
            return
        faults.append(point)
        request = provider.requests[0]
        runtime._require_thread_lock(request.thread_id)
        if stop == "cancel":
            # public cancel也需取锁；这里只改变真实活跃令牌，检验提交前的第二次检查。
            runtime._active[request.turn_id][1].cancel()
        else:
            # 仅推进持久期限计算器的墙钟，不依赖外层asyncio timeout抢先取消任务。
            expired = utc_now() + timedelta(days=2)
            monkeypatch.setattr(approvals, "utc_now", lambda: expired)

    async with AgentRuntime(store, provider, enable_planning=True, fault=fault) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "锁内停止", request_id="r")
    assert faults == ["runtime.before_plan_update_commit"]
    assert turn.status is (TurnStatus.CANCELLED if stop == "cancel" else TurnStatus.FAILED)
    assert turn.error.code == ("cancelled" if stop == "cancel" else "time_budget_exceeded")
    assert not plan_items(turn)
    assert not any(
        isinstance(item.content, ToolResultContent) and item.content.outcome == "succeeded"
        for item in turn.items
    )
    assert len(provider.requests) == 1
    assert all(item.status != ItemStatus.STARTED for item in turn.items)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_reexecuting_a_settled_call_never_publishes_another_plan(tmp_path, cancelled):
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([proposal_step(plan_call()), answer()])
    async with AgentRuntime(store, provider, enable_planning=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "幂等检查", request_id="r")
        assert turn.status is TurnStatus.COMPLETED
        call = next(
            item.content for item in turn.items if isinstance(item.content, ToolCallContent)
        )
        before = await store.get_thread(thread.thread_id)
        events = await store.events(thread.thread_id)
        token = CancelToken()
        if cancelled:
            token.cancel()
        with pytest.raises(TurnCancelled if cancelled else KernelError) as caught:
            await execute_plan_update(runtime, thread.thread_id, turn.turn_id, call, token)
        if not cancelled:
            assert caught.value.code == "tool_call_not_pending"
        assert await store.get_thread(thread.thread_id) == before
        assert await store.events(thread.thread_id) == events
        assert len(plan_items(before.turns[0])) == 1
        assert result_for(before.turns[0]).outcome == "succeeded"


CRASH_POINTS = [
    pytest.param("runtime.before_plan_update_commit", False, id="runtime-pre-commit"),
    pytest.param("session.after_events", False, id="sqlite-events-not-committed"),
    pytest.param("session.after_projection", False, id="sqlite-projection-not-committed"),
    pytest.param("session.after_commit", True, id="sqlite-committed"),
    pytest.param("runtime.after_plan_update_commit", True, id="runtime-post-commit"),
]


@pytest.mark.parametrize("point,committed", CRASH_POINTS)
async def test_plan_and_result_crash_atomically_and_restart_does_not_replay(
    tmp_path, point, committed
):
    database = tmp_path / "session.db"
    store = SQLiteSessionStore(database)
    async with AgentRuntime(store, ScriptedProvider([])) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    log_path = tmp_path / "crash.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.agent.test_planning_tool_recovery",
            str(database),
            str(thread.thread_id),
            point,
            cwd=Path(__file__).parents[2],
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), 15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert returncode == 77, log_path.read_text(encoding="utf-8", errors="replace")
    assert f"crash:{point}" in log_path.read_text(encoding="utf-8", errors="replace")
    crashed = await SQLiteSessionStore(database).get_thread(thread.thread_id)
    crashed_turn = crashed.turns[-1]
    assert len(plan_items(crashed_turn)) == int(committed)
    successes = [
        item.content
        for item in crashed_turn.items
        if isinstance(item.content, ToolResultContent) and item.content.outcome == "succeeded"
    ]
    assert len(successes) == int(committed)
    assert crashed_turn.status is (
        TurnStatus.PREPARING_CONTEXT if committed else TurnStatus.EXECUTING_TOOLS
    )
    if committed:
        assert successes[0].output["plan_item_id"] == str(plan_items(crashed_turn)[0].item_id)
    stable = None
    for _ in range(2):
        reopened = SQLiteSessionStore(database)
        provider = ScriptedProvider([])
        async with AgentRuntime(reopened, provider, enable_planning=True) as runtime:
            restored = await reopened.get_thread(thread.thread_id)
            turn = restored.turns[-1]
            assert turn.status is TurnStatus.INTERRUPTED
            assert turn.error.code == "process_interrupted"
            assert all(item.status != ItemStatus.STARTED for item in turn.items)
            assert len(plan_items(turn)) == int(committed)
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert [result for result in results if result.outcome == "succeeded"] == successes
            if committed:
                assert len(results) == 1
                assert plan_items(turn) == plan_items(crashed_turn)
            assert (
                await runtime.run_turn(thread.thread_id, "离线计划崩溃", request_id="crash") == turn
            )
            assert provider.requests == []
            assert replay(await reopened.events(thread.thread_id)) == restored
            if stable is not None:
                assert restored == stable
            stable = restored


async def _crash_worker(database: str, thread_id: UUID, point: str) -> None:
    writing_plan = False

    def crash(name):
        if name == point and (name.startswith("runtime.") or writing_plan):
            print(f"crash:{name}", flush=True)
            os._exit(77)

    class PlanCrashStore(SQLiteSessionStore):
        async def append(
            self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
        ):
            nonlocal writing_plan
            # SQLite故障只武装真实Plan事务，不在建Thread/用户输入/调用组阶段提前崩溃。
            writing_plan = any(
                isinstance(draft.payload, ItemStarted)
                and isinstance(draft.payload.content, PlanContent)
                for draft in drafts
            )
            try:
                return await super().append(thread_id, drafts, expected_sequence=expected_sequence)
            finally:
                writing_plan = False

    provider = ScriptedProvider([proposal_step(plan_call()), answer()])
    async with AgentRuntime(
        PlanCrashStore(database, fault=crash), provider, enable_planning=True, fault=crash
    ) as runtime:
        await runtime.run_turn(thread_id, "离线计划崩溃", request_id="crash")
    raise AssertionError(f"故障点未触发: {point}")


if __name__ == "__main__":
    asyncio.run(_crash_worker(sys.argv[1], UUID(sys.argv[2]), sys.argv[3]))
