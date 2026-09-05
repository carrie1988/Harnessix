from __future__ import annotations

import hashlib
import json
import sqlite3
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import accounted_answer


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7, 8])
async def test_old_transcript_migrates_without_rewriting_history(
    tmp_path: Path, version: int
) -> None:
    fixture = json.loads((Path(__file__).parent / f"fixtures/session-v{version}.json").read_text())
    store = SQLiteSessionStore(tmp_path / "session.db")
    sql = files("harnessix.session.migrations").joinpath("0001_initial.sql").read_text()
    thread_id = UUID(fixture["snapshot"]["thread_id"])
    snapshot = json.dumps(fixture["snapshot"], ensure_ascii=False)
    originals = [json.dumps(event, ensure_ascii=False) for event in fixture["events"]]
    assert all(event["schema_version"] == version for event in fixture["events"])
    assert any(
        event["payload"].get("content", {}).get("kind") == "tool_call"
        for event in fixture["events"]
    )
    with sqlite3.connect(store.path) as database:
        database.execute("PRAGMA application_id = 1213748043")  # 0x4858534B
        database.execute(
            "CREATE TABLE agent_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        database.executescript(sql)
        database.execute(
            "INSERT INTO agent_migrations VALUES (1, ?)",
            (hashlib.sha256(sql.encode()).hexdigest(),),
        )
        database.execute(
            "INSERT INTO agent_threads VALUES (?, ?, ?, ?)",
            (
                str(thread_id),
                fixture["snapshot"]["sequence"],
                snapshot,
                hashlib.sha256(snapshot.encode()).hexdigest(),
            ),
        )
        database.executemany(
            "INSERT INTO agent_events VALUES (?, ?, ?, ?)",
            [
                (str(thread_id), event["sequence"], event["event_id"], encoded)
                for event, encoded in zip(fixture["events"], originals, strict=True)
            ],
        )
    if version >= 2:
        sql2 = (
            files("harnessix.session.migrations")
            .joinpath("0002_projection_version.sql")
            .read_text()
        )
        with sqlite3.connect(store.path) as database:
            database.executescript(sql2)
            database.execute(
                "INSERT INTO agent_migrations VALUES (2, ?)",
                (hashlib.sha256(sql2.encode()).hexdigest(),),
            )
            database.execute("UPDATE agent_threads SET projection_version = 2")
    if version >= 3:
        sql3 = (
            files("harnessix.session.migrations").joinpath("0003_semantic_contract.sql").read_text()
        )
        with sqlite3.connect(store.path) as database:
            database.executescript(sql3)
            database.execute(
                "INSERT INTO agent_migrations VALUES (3, ?)",
                (hashlib.sha256(sql3.encode()).hexdigest(),),
            )
            database.execute("UPDATE agent_threads SET projection_version = 3")
    if version >= 4:
        sql4 = files("harnessix.session.migrations").joinpath("0004_model_attempts.sql").read_text()
        with sqlite3.connect(store.path) as database:
            database.executescript(sql4)
            database.execute(
                "INSERT INTO agent_migrations VALUES (4, ?)",
                (hashlib.sha256(sql4.encode()).hexdigest(),),
            )
            database.execute("UPDATE agent_threads SET projection_version = 4")
    if version >= 5:
        for number, name in [(5, "0005_response_billing.sql"), (6, "0006_artifacts.sql")]:
            sql = files("harnessix.session.migrations").joinpath(name).read_text()
            with sqlite3.connect(store.path) as database:
                database.executescript(sql)
                database.execute(
                    "INSERT INTO agent_migrations VALUES (?, ?)",
                    (number, hashlib.sha256(sql.encode()).hexdigest()),
                )
                database.execute("UPDATE agent_threads SET projection_version = 5")
    if version >= 6:
        sql = files("harnessix.session.migrations").joinpath("0007_managed_patch.sql").read_text()
        with sqlite3.connect(store.path) as database:
            database.executescript(sql)
            database.execute(
                "INSERT INTO agent_migrations VALUES (7, ?)",
                (hashlib.sha256(sql.encode()).hexdigest(),),
            )
            database.execute("UPDATE agent_threads SET projection_version = 6")
    if version >= 7:
        sql = (
            files("harnessix.session.migrations")
            .joinpath("0008_managed_patch_batch.sql")
            .read_text()
        )
        with sqlite3.connect(store.path) as database:
            database.executescript(sql)
            database.execute(
                "INSERT INTO agent_migrations VALUES (8, ?)",
                (hashlib.sha256(sql.encode()).hexdigest(),),
            )
            database.execute("UPDATE agent_threads SET projection_version = 7")
    if version >= 8:
        sql = (
            files("harnessix.session.migrations")
            .joinpath("0009_batch_diff_artifacts.sql")
            .read_text()
        )
        with sqlite3.connect(store.path) as database:
            database.executescript(sql)
            database.execute(
                "INSERT INTO agent_migrations VALUES (9, ?)",
                (hashlib.sha256(sql.encode()).hexdigest(),),
            )
            database.execute("UPDATE agent_threads SET projection_version = 8")
    await store.initialize()
    assert await store.get_thread(thread_id) == Thread.model_validate_json(snapshot)
    assert await store.get_thread(thread_id) == replay(await store.events(thread_id))
    assert [event.model_dump(mode="json") for event in await store.events(thread_id)] == fixture[
        "events"
    ]
    with sqlite3.connect(store.path) as database:
        assert (
            database.execute("SELECT projection_version FROM agent_threads").fetchone()[0]
            == version
        )
    async with AgentRuntime(store, ScriptedProvider([accounted_answer()])) as runtime:
        completed = await runtime.run_turn(thread_id, "升级后继续", request_id="v8")
    assert completed.status == "completed"
    assert completed.usage.total_tokens == 13 and completed.usage_is_complete
    with sqlite3.connect(store.path) as database:
        assert database.execute(
            "SELECT version FROM agent_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]
        assert database.execute("SELECT projection_version FROM agent_threads").fetchone()[0] == 9
        stored = database.execute(
            "SELECT event_json FROM agent_events ORDER BY sequence"
        ).fetchall()
        assert [row[0] for row in stored[: len(originals)]] == originals
        assert all(json.loads(row[0])["schema_version"] == 9 for row in stored[len(originals) :])
    assert await store.rebuild(thread_id) == await store.get_thread(thread_id)


async def test_unknown_projection_version_fails_closed(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    with sqlite3.connect(store.path) as database:
        database.execute("UPDATE agent_threads SET projection_version = 10")
    with pytest.raises(KernelError) as error:
        await store.get_thread(thread.thread_id)
    assert error.value.code == "projection_too_new"


async def test_second_host_cannot_migrate_before_obtaining_owner_lock(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "new" / "s.db")
    async with AgentRuntime(store, FakeProvider()):

        class UnexpectedMigration(SQLiteSessionStore):
            async def initialize(self):
                raise AssertionError("持锁失败的第二宿主不能触发迁移")

        with pytest.raises(KernelError) as error:
            async with AgentRuntime(UnexpectedMigration(store.path), FakeProvider()):
                pass
        assert error.value.code == "runtime_busy"
