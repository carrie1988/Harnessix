"""恢复Turn的任务在开始执行之前被取消时，Turn同样得到确定性的取消终态。

宿主关闭时取消后台的恢复任务。此前只有进入执行循环之后的取消才会落盘cancelled；取消在取锁、读取
Thread期间到达时Turn停在accepted（2026-11-11 CI负载下
test_service_shutdown_is_bounded_and_persists_cancel复现）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer_support import get_turn
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_runtime import WaitingProvider


async def test_a_resume_cancelled_before_it_starts_settles_the_turn_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, ScriptedProvider([answer("不会执行到这里")])) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.accept_turn(thread.thread_id, "任务", request_id="accept")
        original = store.get_thread
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(thread_id):
            entered.set()
            await release.wait()
            return await original(thread_id)

        monkeypatch.setattr(store, "get_thread", blocked)
        task = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        await entered.wait()
        monkeypatch.setattr(store, "get_thread", original)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        settled = get_turn(await store.get_thread(thread.thread_id), turn.turn_id)
        assert settled.status is TurnStatus.CANCELLED
        assert settled.error is not None and settled.error.code == "cancelled"


async def test_a_second_resume_cancelled_early_leaves_the_running_turn_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = WaitingProvider()
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.accept_turn(thread.thread_id, "任务", request_id="accept")
        running = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        await provider.waiting.wait()
        original = store.get_thread
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(thread_id):
            entered.set()
            await release.wait()
            return await original(thread_id)

        monkeypatch.setattr(store, "get_thread", blocked)
        second = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        await entered.wait()
        monkeypatch.setattr(store, "get_thread", original)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

        # 另一个任务正在执行这一轮：早到的取消不能替它结束。
        current = get_turn(await store.get_thread(thread.thread_id), turn.turn_id)
        assert current.status is not TurnStatus.CANCELLED
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        final = get_turn(await store.get_thread(thread.thread_id), turn.turn_id)
        assert final.status is TurnStatus.CANCELLED


async def test_an_early_cancelled_resume_leaves_a_turn_waiting_for_approval_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, RecordingTools(approval=True)) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="approval")
        assert turn.status is TurnStatus.WAITING_APPROVAL
        original = store.get_thread
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(thread_id):
            entered.set()
            await release.wait()
            return await original(thread_id)

        monkeypatch.setattr(store, "get_thread", blocked)
        task = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        await entered.wait()
        monkeypatch.setattr(store, "get_thread", original)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 等待审批的一轮下次打开还能继续：早到的取消不结束它。
        assert get_turn(await store.get_thread(thread.thread_id), turn.turn_id) == turn
