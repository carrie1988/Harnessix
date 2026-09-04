import hashlib
import json
import sqlite3
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.reducer import replay
from harnessix.session.sqlite import SQLiteSessionStore


def legacy_database(path):
    """使用真实旧 wheel 导出记录及冻结1–7迁移，作为故障注入夹具。"""
    fixture = json.loads((Path(__file__).parent / "fixtures/session-v6.json").read_text())
    events = [json.dumps(event, ensure_ascii=False) for event in fixture["events"]]
    snapshot = json.dumps(fixture["snapshot"], ensure_ascii=False)
    thread_id = fixture["snapshot"]["thread_id"]
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA application_id = 1213748043")
        db.execute(
            "CREATE TABLE agent_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        for item in sorted(files("harnessix.session.migrations").iterdir(), key=lambda p: p.name):
            if not item.name.endswith(".sql") or int(item.name.split("_")[0]) > 7:
                continue
            sql = item.read_text()
            db.executescript(sql)
            db.execute(
                "INSERT INTO agent_migrations VALUES (?,?)",
                (int(item.name.split("_")[0]), hashlib.sha256(sql.encode()).hexdigest()),
            )
        db.execute(
            "INSERT INTO agent_threads VALUES (?,?,?,?,6)",
            (
                thread_id,
                fixture["snapshot"]["sequence"],
                snapshot,
                hashlib.sha256(snapshot.encode()).hexdigest(),
            ),
        )
        db.executemany(
            "INSERT INTO agent_events VALUES (?,?,?,?)",
            [
                (thread_id, e["sequence"], e["event_id"], raw)
                for e, raw in zip(fixture["events"], events, strict=True)
            ],
        )
    return UUID(thread_id), events, snapshot


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
async def test_real_migration8_exit_preserves_old_bytes(tmp_path, point):
    import asyncio

    path = tmp_path / "s.db"
    thread_id, events, snapshot = legacy_database(path)
    code = """
import asyncio, os, sys, aiosqlite
from harnessix.session.sqlite import SQLiteSessionStore
original = aiosqlite.Connection.execute
async def execute(self, sql, parameters=None):
    result = await original(self, sql, parameters)
    if (
        sql.startswith("INSERT INTO agent_migrations")
        and parameters[0] == 8
        and sys.argv[2] == "before_commit"
    ):
        os._exit(80)
    return result
aiosqlite.Connection.execute = execute
class Store(SQLiteSessionStore):
    async def _enable_wal(self):
        if sys.argv[2] == "after_commit":
            os._exit(80)
        await super()._enable_wal()
asyncio.run(Store(sys.argv[1]).initialize())
raise AssertionError("未到达迁移退出点")
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(path), point],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 80, child.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == (
            7 if point == "before_commit" else 8
        )
        assert [
            r[0] for r in db.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ] == events
        assert db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0] == snapshot
    store = SQLiteSessionStore(path)
    await store.initialize()
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT projection_version FROM agent_threads").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == 8
        assert db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0] == snapshot


async def test_migration8_failed_insert_rolls_back_marker(tmp_path, monkeypatch):
    import aiosqlite

    path = tmp_path / "s.db"
    _, events, snapshot = legacy_database(path)
    original = aiosqlite.Connection.execute

    async def fail(self, sql, parameters=None):
        result = await original(self, sql, parameters)
        if sql.startswith("INSERT INTO agent_migrations") and parameters[0] == 8:
            raise sqlite3.OperationalError("迁移存储故障")
        return result

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail)
    with pytest.raises(KernelError):
        await SQLiteSessionStore(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == 7
        assert db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0] == snapshot
        assert [
            r[0] for r in db.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ] == events
