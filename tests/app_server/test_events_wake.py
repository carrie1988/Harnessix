"""events/next的唤醒（ADR 0168）：Turn停下来等客户端或结束时立即交付，其余追加仍成批取走。"""

from __future__ import annotations

import asyncio

from harnessix.agent.models import Thread, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server import query_runtime
from harnessix.app_server.query_runtime import ThreadSignals
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import EventsNextParams
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.append_signal import AppendSignal
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_approvals import APPROVE, approval


def _service(runtime: AgentRuntime) -> AgentApplicationService:
    return AgentApplicationService(
        runtime, runtime.store, SQLiteProtocolRequestStore(runtime.store.path)
    )


async def _parked_long_poll(service, store, monkeypatch, thread_id, after_cursor):
    """发起events/next并等它读完两次空快照、停在信号上；此后只有唤醒能让它返回。"""
    reads = 0
    parked = asyncio.Event()
    original = store.events

    async def counted(*args, **kwargs):
        nonlocal reads
        try:
            return await original(*args, **kwargs)
        finally:
            reads += 1
            if reads == 2:
                parked.set()

    with monkeypatch.context() as patch:
        patch.setattr(store, "events", counted)
        waiter = asyncio.create_task(
            service.next_events(
                EventsNextParams(thread_id=thread_id, after_cursor=after_cursor, wait_ms=30000)
            )
        )
        await asyncio.wait_for(parked.wait(), 5)
    await asyncio.sleep(0)
    assert not waiter.done()
    return waiter


async def test_long_poll_delivers_the_approval_request_without_waiting_for_the_timed_reread(
    tmp_path, monkeypatch
):
    # 定时重读在本用例内不会触发：等待者只能被追加通知唤醒。
    monkeypatch.setattr(query_runtime, "_REREAD_SECONDS", 3600.0)
    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, tools) as runtime:
        service = _service(runtime)
        thread = await runtime.create_thread(str(tmp_path))
        waiter = await _parked_long_poll(
            service, store, monkeypatch, thread.thread_id, thread.sequence
        )

        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        assert turn.status == TurnStatus.WAITING_APPROVAL
        page = await asyncio.wait_for(waiter, 5)

        # 之前的提交（Turn开始、状态变化、模型回复）没有唤醒等待者，所以这一页直接读到审批为止。
        waiting = await store.get_thread(thread.thread_id)
        assert page.replay.scanned_through == waiting.sequence and not page.timed_out
        last = page.replay.events[-1].data
        assert (last.type, last.status) == ("turn_state_changed", "waiting_approval")
        kinds = [
            event.data.item.content.kind
            for event in page.replay.events
            if event.data.type == "item_started"
        ]
        assert kinds[-1] == "approval_request"

        await service.close()


async def test_only_commits_that_leave_the_turn_waiting_on_the_client_or_ended_wake_readers(
    tmp_path,
):
    store = SQLiteSessionStore(tmp_path / "s.db")
    running: list[Thread] = []

    class Inspect(RecordingTools):
        async def execute(self, call, cancel):
            running.append(await store.get_thread(thread.thread_id))
            return await super().execute(call, cancel)

    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, Inspect(approval=True)) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        waiting = await store.get_thread(thread.thread_id)
        request = approval(turn)
        await runtime.reply_approval(
            thread.thread_id,
            turn.turn_id,
            request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=APPROVE,
        )
        await runtime.resume_turn(thread.thread_id, turn.turn_id)
        ended = await store.get_thread(thread.thread_id)

    appends = AppendSignal()
    signals = ThreadSignals(appends)
    event = signals.setdefault(thread.thread_id, asyncio.Event())
    (executing,) = running
    assert executing.turns[-1].status == TurnStatus.EXECUTING_TOOLS
    appends.notify(executing)
    assert not event.is_set()
    appends.notify(waiting)
    assert event.is_set()
    event.clear()
    appends.notify(ended)
    assert event.is_set() and ended.active_turn_id is None

    # 没有等待者的Thread不建信号；关闭后不再接收通知。
    signals.pop(thread.thread_id)
    appends.notify(waiting)
    assert not signals
    signals.close()
    signals.setdefault(thread.thread_id, asyncio.Event())
    appends.notify(waiting)
    assert not signals[thread.thread_id].is_set()
