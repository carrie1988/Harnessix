"""Session追加通知（ADR 0168）：提交之后才通知，重复批次不通知，订阅者故障不影响写入。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, Thread, ThreadArchived, ThreadCreated
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer


async def test_each_committed_append_notifies_with_the_thread_that_readers_will_see(tmp_path):
    store = SQLiteSessionStore(tmp_path / "s.db")
    notified: list[Thread] = []
    store.appends.subscribe(notified.append)
    async with AgentRuntime(store, ScriptedProvider([answer()])) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "任务", request_id="r")

    stored = await store.get_thread(thread.thread_id)
    # 每次提交通知一次，序号逐次前进；最后一次通知的Thread就是此刻读到的Thread。
    sequences = [item.sequence for item in notified]
    assert sequences == sorted(set(sequences)) and sequences[0] == 1
    assert notified[-1] == stored and stored.active_turn_id is None


async def test_notification_comes_after_the_commit_and_not_for_a_failed_append(tmp_path):
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.initialize()
    visible: list[int] = []

    def read_back(thread: Thread) -> None:
        # 通知时另开连接读取，必须已经看得到这次追加。
        with closing(sqlite3.connect(store.path)) as database:
            row = database.execute(
                "SELECT sequence FROM agent_threads WHERE thread_id = ?", (str(thread.thread_id),)
            ).fetchone()
        visible.append(row[0])

    store.appends.subscribe(read_back)
    created = EventDraft(payload=ThreadCreated(workspace=str(tmp_path)))
    thread = await store.append(uuid4(), [created], expected_sequence=0)
    assert visible == [1]

    with pytest.raises(KernelError) as conflict:
        await store.append(
            thread.thread_id,
            [EventDraft(payload=ThreadArchived(reason="归档"))],
            expected_sequence=7,
        )
    assert conflict.value.code == "sequence_conflict" and visible == [1]


async def test_repeated_batch_does_not_notify_and_a_failing_subscriber_does_not_fail_the_append(
    tmp_path,
):
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.initialize()
    notified: list[int] = []

    def broken(_thread: Thread) -> None:
        raise RuntimeError("订阅者故障")

    store.appends.subscribe(broken)
    unsubscribe = store.appends.subscribe(lambda thread: notified.append(thread.sequence))
    thread_id = uuid4()
    created = EventDraft(payload=ThreadCreated(workspace=str(tmp_path)))
    first = await store.append(thread_id, [created], expected_sequence=0)
    # 同一批次重放是幂等的：返回原结果，没有新事件，所以不再通知。
    assert await store.append(thread_id, [created], expected_sequence=0) == first
    assert notified == [1]

    unsubscribe()
    await store.append(
        first.thread_id, [EventDraft(payload=ThreadArchived(reason="归档"))], expected_sequence=1
    )
    assert notified == [1]
