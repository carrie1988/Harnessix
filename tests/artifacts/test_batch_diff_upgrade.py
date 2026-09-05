import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.reducer import replay
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.session.sqlite import SQLiteSessionStore


def old_database(path):
    fixture = json.loads((Path(__file__).parent / "fixtures/session-v7-artifact.json").read_text())
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA application_id=1213748043")
        db.execute(
            "CREATE TABLE agent_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        for migration in sorted(
            files("harnessix.session.migrations").iterdir(), key=lambda p: p.name
        ):
            if not migration.name.endswith(".sql") or int(migration.name[:4]) > 8:
                continue
            sql = migration.read_text()
            db.executescript(sql)
            db.execute(
                "INSERT INTO agent_migrations VALUES (?,?)",
                (int(migration.name[:4]), hashlib.sha256(sql.encode()).hexdigest()),
            )
        db.execute("INSERT INTO agent_threads VALUES (?,?,?,?,?)", fixture["snapshot"])
        for raw in fixture["events"]:
            e = json.loads(raw)
            db.execute(
                "INSERT INTO agent_events VALUES (?,?,?,?)",
                (e["thread_id"], e["sequence"], e["event_id"], raw),
            )
        row = fixture["artifact"]
        db.execute(
            "INSERT INTO agent_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*row[:-1], bytes.fromhex(row[-1])),
        )
    return fixture


@pytest.mark.parametrize("point", ["copied", "dropped", "renamed", "after_commit"])
async def test_real_migration9_exit_preserves_original_artifact_and_events(
    tmp_path, monkeypatch, point
):
    path = tmp_path / "s.db"
    fixture = old_database(path)
    code = """
import asyncio, os, sys, aiosqlite
from harnessix.session.sqlite import SQLiteSessionStore
original = aiosqlite.Connection.execute
async def execute(self, sql, parameters=None):
    result = await original(self, sql, parameters)
    stop = {
        "copied": "INSERT INTO agent_artifacts_v9",
        "dropped": "DROP TABLE agent_artifacts",
        "renamed": "ALTER TABLE agent_artifacts_v9",
    }
    if sys.argv[2] in stop and sql.strip().startswith(stop[sys.argv[2]]):
        os._exit(83)
    return result
aiosqlite.Connection.execute = execute
class Store(SQLiteSessionStore):
    async def _enable_wal(self):
        if sys.argv[2] == "after_commit": os._exit(83)
        await super()._enable_wal()
asyncio.run(Store(sys.argv[1]).initialize())
raise AssertionError("未到达退出点")
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(path), point],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 83, child.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == (
            11 if point == "after_commit" else 8
        )
        assert list(db.execute("SELECT * FROM agent_threads").fetchone()) == fixture["snapshot"]
        assert [
            r[0] for r in db.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ] == fixture["events"]
    store = SQLiteSessionStore(path)
    await store.initialize()
    row = fixture["artifact"]
    ref = ArtifactRef.model_validate_json(row[5])
    monkeypatch.setattr(
        "harnessix.artifacts.sqlite.utc_now", lambda: ref.expires_at - timedelta(seconds=1)
    )
    artifacts = SQLiteArtifactStore(store)
    page = await artifacts.read(UUID(row[1]), row[4], ref.artifact_id)
    assert page.text.encode() == bytes.fromhex(row[-1]) and page.artifact == ref
    assert replay(await store.events(UUID(row[1]))) == await store.get_thread(UUID(row[1]))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT purpose FROM agent_artifacts").fetchone()[0] == "tool_result"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        # 旧只读调用仍不能重复归档；其他两种用途不是放宽旧接口。
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO agent_artifacts SELECT ?,thread_id,turn_id,call_id,workspace_scope,"
                "manifest_json,size_bytes,expires_at,state,body,purpose FROM agent_artifacts",
                (str(uuid4()),),
            )
    monkeypatch.setattr(
        "harnessix.artifacts.sqlite.utc_now", lambda: ref.expires_at + timedelta(seconds=1)
    )
    assert (await artifacts.collect()).expired == 1
    with pytest.raises(KernelError) as error:
        await artifacts.read(UUID(row[1]), row[4], ref.artifact_id)
    assert error.value.code == "artifact_expired"
