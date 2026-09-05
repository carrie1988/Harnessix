from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.reducer import replay
from harnessix.session.sqlite import SQLiteSessionStore

_SESSION_MIGRATION_SHA256 = (
    "f511dc6c2c86db4995b8a68b7812d8e6072aa82a1f19b0a559e0975a9e7d2c95",
    "043a8da8a557c8eaf18562d0e1679eb7fb56d1203b54cf866fb4d6684e6a0a33",
    "3c9b395782a2fc000382dd6cdfb2f215c9b04b96b7d0b2a0ab06657af9813b04",
    "301f45039593a6a75237ee1c5e7a0185a71d3603cac589b7b815af1410bfc04d",
    "1ef506e410816cb889d50957c7860a0907635d473d790ebcc632a959d4fd8a85",
    "b9189c639e65e67c93b0bfc5e8158ef4acf9b3392da2435d781aa36f97b431a0",
    "251f0b9b7c87413cab9488cd751541eead40d3a5d76279e9095ebd61fd57b847",
    "a6b6733ad430adf1bc2a41b69492d6376b551278ff75b2ac6c1dc95c2443c6ca",
    "67f19391a613b5525a5404732cc2a2429337fb8a8c5ae10f035c4ee98c5227a7",
    "fbcda6a8f05001fb1834aae2c75ed8e96d052632c8627777b62dabd5edb5b3fa",
)


def legacy_v8_database(path: Path) -> tuple[UUID, list[str], str]:
    """真实 e0e8498 wheel 导出的 v8 transcript；手工建库仅用于退出点注入。"""
    fixture_path = Path(__file__).parent / "fixtures/session-v8.json"
    assert (
        hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        == "f8c5413a0d0af920b6c1fcd4e7e286fb14b000045a5832b29663c26c11f02cc3"
    )
    fixture = json.loads(fixture_path.read_text())
    events = [json.dumps(event, ensure_ascii=False) for event in fixture["events"]]
    snapshot = json.dumps(fixture["snapshot"], ensure_ascii=False)
    thread_id = UUID(fixture["snapshot"]["thread_id"])
    root = files("harnessix.session.migrations")
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA application_id = 1213748043")
        database.execute(
            "CREATE TABLE agent_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        for migration in sorted(root.iterdir(), key=lambda item: item.name):
            if not migration.name.endswith(".sql"):
                continue
            version = int(migration.name.split("_", 1)[0])
            if version > 9:
                continue
            sql = migration.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            assert checksum == _SESSION_MIGRATION_SHA256[version - 1]
            database.executescript(sql)
            database.execute(
                "INSERT INTO agent_migrations VALUES (?, ?)",
                (version, checksum),
            )
        database.execute(
            "INSERT INTO agent_threads VALUES (?, ?, ?, ?, 8)",
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
                for event, encoded in zip(fixture["events"], events, strict=True)
            ],
        )
    return thread_id, events, snapshot


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
async def test_real_migration10_exit_preserves_v8_bytes(tmp_path: Path, point: str) -> None:
    path = tmp_path / "session.db"
    thread_id, events, snapshot = legacy_v8_database(path)
    inode = path.stat().st_ino
    code = """
import asyncio, os, sys, aiosqlite
from harnessix.session.sqlite import SQLiteSessionStore
original = aiosqlite.Connection.execute
async def execute(self, sql, parameters=None):
    result = await original(self, sql, parameters)
    if (
        sql.startswith("INSERT INTO agent_migrations")
        and parameters[0] == 10
        and sys.argv[2] == "before_commit"
    ):
        os._exit(85)
    return result
aiosqlite.Connection.execute = execute
class Store(SQLiteSessionStore):
    async def _enable_wal(self):
        if sys.argv[2] == "after_commit":
            os._exit(85)
        await super()._enable_wal()
asyncio.run(Store(sys.argv[1]).initialize())
raise AssertionError("未到达 migration10 退出点")
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(path), point],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 85, child.stderr
    assert path.stat().st_ino == inode
    with sqlite3.connect(path) as database:
        migration_count = 9 if point == "before_commit" else 10
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            database.execute(
                "SELECT version,checksum FROM agent_migrations ORDER BY version"
            ).fetchall()
            == list(enumerate(_SESSION_MIGRATION_SHA256, start=1))[:migration_count]
        )
        assert database.execute("SELECT projection_version FROM agent_threads").fetchone()[0] == 8
        assert database.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0] == snapshot
        assert [
            row[0]
            for row in database.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ] == events

    store = SQLiteSessionStore(path)
    await store.initialize()
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    with sqlite3.connect(path) as database:
        assert database.execute(
            "SELECT version,checksum FROM agent_migrations ORDER BY version"
        ).fetchall() == list(enumerate(_SESSION_MIGRATION_SHA256, start=1))
        assert database.execute("SELECT projection_version FROM agent_threads").fetchone()[0] == 8
        assert database.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0] == snapshot
        assert [
            row[0]
            for row in database.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ] == events
