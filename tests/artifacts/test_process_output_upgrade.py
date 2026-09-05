from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.reducer import replay
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.session.sqlite import SQLiteSessionStore


def _version10_database(path: Path) -> dict[str, object]:
    fixture = json.loads((Path(__file__).parent / "fixtures/session-v7-artifact.json").read_text())
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA application_id=1213748043")
        database.execute(
            "CREATE TABLE agent_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        for migration in sorted(
            files("harnessix.session.migrations").iterdir(), key=lambda item: item.name
        ):
            if not migration.name.endswith(".sql"):
                continue
            version = int(migration.name[:4])
            if version > 10:
                continue
            sql = migration.read_text()
            database.executescript(sql)
            database.execute(
                "INSERT INTO agent_migrations VALUES (?, ?)",
                (version, hashlib.sha256(sql.encode()).hexdigest()),
            )
        database.execute("INSERT INTO agent_threads VALUES (?,?,?,?,?)", fixture["snapshot"])
        for raw in fixture["events"]:
            event = json.loads(raw)
            database.execute(
                "INSERT INTO agent_events VALUES (?,?,?,?)",
                (event["thread_id"], event["sequence"], event["event_id"], raw),
            )
        row = fixture["artifact"]
        database.execute(
            "INSERT INTO agent_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*row[:-1], bytes.fromhex(row[-1]), "tool_result"),
        )
    return fixture


@pytest.mark.parametrize("point", ["copied", "dropped", "renamed", "after_commit"])
async def test_migration11_exit_is_atomic_and_preserves_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    path = tmp_path / "session.db"
    fixture = _version10_database(path)
    inode = path.stat().st_ino
    code = """
import asyncio, os, sys, aiosqlite
from harnessix.session.sqlite import SQLiteSessionStore
original = aiosqlite.Connection.execute
async def execute(self, sql, parameters=None):
    result = await original(self, sql, parameters)
    stop = {
        "copied": "INSERT INTO agent_artifacts_v11",
        "dropped": "DROP TABLE agent_artifacts",
        "renamed": "ALTER TABLE agent_artifacts_v11",
    }
    if sys.argv[2] in stop and sql.strip().startswith(stop[sys.argv[2]]):
        os._exit(86)
    return result
aiosqlite.Connection.execute = execute
class Store(SQLiteSessionStore):
    async def _enable_wal(self):
        if sys.argv[2] == "after_commit": os._exit(86)
        await super()._enable_wal()
asyncio.run(Store(sys.argv[1]).initialize())
raise AssertionError("未到达 migration11 退出点")
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(path), point],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 86, child.stderr
    assert path.stat().st_ino == inode
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert database.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == (
            11 if point == "after_commit" else 10
        )
        assert (
            list(database.execute("SELECT * FROM agent_threads").fetchone()) == fixture["snapshot"]
        )
        assert [
            row[0]
            for row in database.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ] == fixture["events"]

    store = SQLiteSessionStore(path)
    await store.initialize()
    row = fixture["artifact"]
    ref = ArtifactRef.model_validate_json(row[5])
    monkeypatch.setattr(
        "harnessix.artifacts.sqlite.utc_now",
        lambda: ref.expires_at - timedelta(seconds=1),
    )
    artifacts = SQLiteArtifactStore(store)
    page = await artifacts.read(UUID(row[1]), row[4], ref.artifact_id)
    assert page.text.encode() == bytes.fromhex(row[-1])
    assert page.artifact == ref
    assert replay(await store.events(UUID(row[1]))) == await store.get_thread(UUID(row[1]))
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT purpose FROM agent_artifacts").fetchone()[0] == (
            "tool_result"
        )
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("UPDATE agent_artifacts SET purpose = 'unknown'")

    monkeypatch.setattr(
        "harnessix.artifacts.sqlite.utc_now",
        lambda: ref.expires_at + timedelta(seconds=1),
    )
    assert (await artifacts.collect()).expired == 1
    with pytest.raises(KernelError) as error:
        await artifacts.read(UUID(row[1]), row[4], ref.artifact_id)
    assert error.value.code == "artifact_expired"
