"""追加的期望序号取自投影行（ADR 0168 D4）：与完整读取得到的序号相同，追加事务内的完整核对不变。"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, ThreadArchived
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.session.test_authenticated_history import binding, business_rows, seed


def _archive(reason: str = "done") -> list[EventDraft]:
    return [EventDraft(payload=ThreadArchived(reason=reason))]


def _called_from_runtime_append() -> bool:
    """调用栈（含await链）里是否有AgentRuntime的锁内追加（_commit与尝试事实的合并提交都经过它）。"""
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code is AgentRuntime._append_payloads.__code__:
            return True
        frame = frame.f_back
    return False


@pytest.mark.parametrize("authenticated", [False, True])
async def test_committed_sequence_equals_the_sequence_of_a_full_read(tmp_path, authenticated):
    with binding() as proof:
        store, tid, created = await seed(tmp_path / "state.db", proof if authenticated else None)
        assert await store.committed_sequence(tid) == created.sequence == 1

        archived = await store.append(tid, _archive(), expected_sequence=1)

        assert await store.committed_sequence(tid) == archived.sequence == 2
        assert (await store.get_thread(tid)).sequence == 2


@pytest.mark.parametrize("authenticated", [False, True])
async def test_without_a_projection_row_the_light_read_fails_like_a_full_read(
    tmp_path, authenticated
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof if authenticated else None)
        unknown = uuid4()
        with pytest.raises(KernelError) as full:
            await store.get_thread(unknown)
        with pytest.raises(KernelError) as light:
            await store.committed_sequence(unknown)
        assert light.value.code == full.value.code == "thread_not_found"

        # 事件还在、投影行不见了：改走完整读取，给出同一个错误码。
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("DELETE FROM agent_threads")
        with pytest.raises(KernelError) as full:
            await store.get_thread(tid)
        with pytest.raises(KernelError) as light:
            await store.committed_sequence(tid)
        assert light.value.code == full.value.code


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("fault", ["row_sequence", "projection_bytes"])
async def test_append_after_the_light_read_still_rejects_what_a_full_read_rejects(
    tmp_path, authenticated, fault
):
    """轻查询不核对投影，按行内序号给出期望值；被改写的投影在追加事务内按完整读取的同一错误码拒绝，
    不写入任何行。"""
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof if authenticated else None)
        with closing(sqlite3.connect(store.path)) as db, db:
            if fault == "row_sequence":
                db.execute("UPDATE agent_threads SET sequence=7")
            else:
                db.execute("UPDATE agent_threads SET snapshot_json=snapshot_json || ' '")
        before = business_rows(store.path)
        with pytest.raises(KernelError) as full:
            await store.get_thread(tid)

        expected = await store.committed_sequence(tid)
        with pytest.raises(KernelError) as appended:
            await store.append(tid, _archive(), expected_sequence=expected)

        assert expected == (7 if fault == "row_sequence" else 1)
        assert appended.value.code == full.value.code
        assert business_rows(store.path) == before


async def test_runtime_commits_with_the_light_sequence_and_it_always_equals_a_full_read(
    tmp_path, monkeypatch
):
    """产品运行中每次提交前都另做一次完整读取比对：轻查询给出的期望序号与完整读取的序号逐次相同，
    且AgentRuntime的锁内追加不再调用get_thread。"""
    with binding() as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        light = store.committed_sequence
        full = SQLiteSessionStore.get_thread
        compared: list[int] = []
        full_reads_in_commit: list[int] = []

        async def compared_sequence(thread_id):
            sequence = await light(thread_id)
            assert sequence == (await full(store, thread_id)).sequence
            compared.append(sequence)
            return sequence

        async def get_thread(thread_id):
            if _called_from_runtime_append():
                full_reads_in_commit.append(1)
            return await full(store, thread_id)

        monkeypatch.setattr(store, "committed_sequence", compared_sequence)
        monkeypatch.setattr(store, "get_thread", get_thread)
        steps = 3
        provider = ScriptedProvider([*(tool_step("test.read") for _ in range(steps)), answer()])
        async with AgentRuntime(store, provider, RecordingTools()) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "read", request_id="light-sequence")

        assert turn.status == "completed" and turn.model_steps == steps + 1
        assert full_reads_in_commit == []
        assert len(compared) > 4 * steps and compared == sorted(compared)
