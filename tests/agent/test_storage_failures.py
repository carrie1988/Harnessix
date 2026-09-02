from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from harnessix.agent.errors import FailureCategory, KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.errors import storage_errors
from harnessix.session.sqlite import SQLiteSessionStore


@pytest.mark.parametrize("corruption", ["gap", "index", "json", "snapshot", "orphan"])
async def test_physical_corruption_fails_closed(tmp_path: Path, corruption: str) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    before = await store.get_thread(thread.thread_id)
    with sqlite3.connect(store.path) as database:
        if corruption == "gap":
            database.execute("DELETE FROM agent_events WHERE sequence = 3")
        elif corruption == "index":
            database.execute("UPDATE agent_events SET event_id = 'broken' WHERE sequence = 3")
        elif corruption == "json":
            database.execute("UPDATE agent_events SET event_json = 'not-json' WHERE sequence = 3")
        elif corruption == "snapshot":
            # 即便校验值匹配，结构损坏也必须转成公开错误。
            database.execute(
                "UPDATE agent_threads SET snapshot_json = '{}', snapshot_sha256 = ?",
                (hashlib.sha256(b"{}").hexdigest(),),
            )
        else:
            database.execute("DELETE FROM agent_events")
    if corruption in {"snapshot", "gap", "orphan"}:
        with pytest.raises(KernelError) as error:
            await store.get_thread(thread.thread_id)
    else:
        with pytest.raises(KernelError) as error:
            await store.events(thread.thread_id)
    assert error.value.to_failure().category == FailureCategory.STORAGE
    if corruption == "snapshot":
        assert await store.rebuild(thread.thread_id) == before
    else:
        with pytest.raises(KernelError):
            await store.rebuild(thread.thread_id)
    if corruption == "orphan":
        assert await store.thread_ids() == [thread.thread_id]
        with pytest.raises(KernelError):
            async with AgentRuntime(store, FakeProvider()):
                pass


async def test_actual_sqlite_readonly_and_full_errors_are_normalized(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.initialize()
    with pytest.raises(KernelError) as error:
        async with store._connection() as database:
            await database.execute("PRAGMA query_only = ON")
            await database.execute("CREATE TABLE disallowed (x)")
    assert error.value.code == "storage_unavailable"
    with pytest.raises(KernelError) as error:
        async with store._connection() as database:
            await database.execute("PRAGMA max_page_count = 1")
            await database.execute("CREATE TABLE full_fixture (payload BLOB)")
            await database.execute("INSERT INTO full_fixture VALUES (zeroblob(1000000))")
    assert error.value.code == "storage_full"
    # SQLite FULL 不得留下一半的 Session 事实，修复条件后初始化仍可完成。
    await store.initialize()
    assert await store.thread_ids() == []


@pytest.mark.parametrize(
    "code, expected, retryable",
    [
        (sqlite3.SQLITE_BUSY, "storage_busy", True),
        (sqlite3.SQLITE_LOCKED, "storage_busy", True),
        (sqlite3.SQLITE_CORRUPT, "database_corrupt", False),
        (sqlite3.SQLITE_CANTOPEN, "storage_unavailable", False),
    ],
)
def test_driver_error_mapping_never_exposes_raw_message(code, expected, retryable) -> None:
    raw = sqlite3.OperationalError("private-canary SQL path credential")
    raw.sqlite_errorcode = code
    with pytest.raises(KernelError) as error:
        with storage_errors():
            raise raw
    assert error.value.code == expected
    assert error.value.retryable == retryable
    assert "private-canary" not in str(error.value)
    assert error.value.__suppress_context__


async def test_invalid_database_file_is_structured_error(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    path.write_text("not a sqlite database")
    with pytest.raises(KernelError) as error:
        await SQLiteSessionStore(path).initialize()
    assert error.value.code == "database_corrupt"


async def test_corrupt_idempotency_lookup_is_not_raw_validation_error(tmp_path: Path) -> None:
    from harnessix.agent.models import EventDraft

    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    event = (await store.events(thread.thread_id))[0]
    draft = EventDraft.model_validate(event.model_dump(exclude={"thread_id", "sequence"}))
    with sqlite3.connect(store.path) as database:
        database.execute("UPDATE agent_events SET event_json = 'private-canary-invalid-json'")
    with pytest.raises(KernelError) as error:
        await store.append(thread.thread_id, [draft], expected_sequence=0)
    assert error.value.code == "event_corrupt"
    assert "private-canary" not in str(error.value)


async def test_invalid_batch_is_structured_and_atomic(tmp_path: Path) -> None:
    from harnessix.agent.ids import new_id
    from harnessix.agent.models import EventDraft, ThreadCreated

    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.initialize()
    valid = EventDraft(payload=ThreadCreated(workspace="/workspace"))
    invalid = valid.model_copy(update={"schema_version": 999})
    with pytest.raises(KernelError) as error:
        await store.append(new_id(), [invalid], expected_sequence=0)
    assert error.value.code == "invalid_batch"
    assert await store.thread_ids() == []
