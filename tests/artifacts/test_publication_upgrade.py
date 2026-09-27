"""独立27版程序生成真实Artifact；28版迁移保留原字节，不追认旧正文保护。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_publication import protected

OLD_REVISION = "3c5f6e36d9c9ce98709c5c37ae2316709c443001"
PROGRAM = """
import asyncio,json,sys,sqlite3
from pathlib import Path
import harnessix.artifacts.sqlite as implementation
from tests.artifacts.helpers import exercise,results
assert Path(implementation.__file__).is_relative_to(Path.cwd())
async def main():
 parent=Path(sys.argv[1]);parent.mkdir()
 store,artifacts,scope,thread,turn=await exercise(parent)
 with sqlite3.connect(store.path) as db:
  assert db.execute('SELECT MAX(version) FROM agent_migrations').fetchone()[0]==27
 ref=results(turn)[0].output['artifact']
 print(json.dumps({'module':implementation.__file__,'scope':scope,'thread_id':str(thread.thread_id),'ref':ref}))
asyncio.run(main())
"""


def database_facts(path):
    with sqlite3.connect(path) as db:
        return (
            db.execute("SELECT * FROM agent_threads ORDER BY thread_id").fetchall(),
            db.execute("SELECT * FROM agent_events ORDER BY thread_id,sequence").fetchall(),
            db.execute(
                "SELECT artifact_id,thread_id,turn_id,call_id,workspace_scope,manifest_json,"
                "size_bytes,expires_at,state,body,purpose,created_at FROM agent_artifacts"
            ).fetchall(),
        )


@pytest.fixture
def old_record(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    archive = tmp_path / "old-binary"
    archive.mkdir()
    raw = subprocess.run(
        [
            "git",
            "archive",
            OLD_REVISION,
            "src",
            "tests/__init__.py",
            "tests/agent/__init__.py",
            "tests/agent/helpers.py",
            "tests/artifacts/__init__.py",
            "tests/artifacts/helpers.py",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=30,
    )
    with tarfile.open(fileobj=io.BytesIO(raw.stdout)) as source:
        source.extractall(archive, filter="data")
    parent = tmp_path / "state"
    child = subprocess.run(
        [sys.executable, "-c", PROGRAM, str(parent)],
        cwd=archive,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(archive / "src"), str(archive)))),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    record = json.loads(child.stdout)
    assert Path(record["module"]).is_relative_to(archive)
    return parent / "session.db", record


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
async def test_real_old_artifact_migration28_is_atomic_and_not_authorized(old_record, point):
    path, record = old_record
    before = database_facts(path)
    code = """
import asyncio,os,sys,aiosqlite
from harnessix.session.sqlite import SQLiteSessionStore
original=aiosqlite.Connection.execute
async def execute(self,sql,parameters=None):
 result=await original(self,sql,parameters)
 if (sql.startswith('INSERT INTO agent_migrations')
     and parameters[0]==28 and sys.argv[2]=='before_commit'):
  os._exit(87)
 return result
aiosqlite.Connection.execute=execute
class Store(SQLiteSessionStore):
 async def _enable_wal(self):
  if sys.argv[2]=='after_commit':os._exit(87)
  await super()._enable_wal()
asyncio.run(Store(sys.argv[1]).initialize())
raise AssertionError('未到达28版迁移退出点')
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(path), point],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 87, child.stderr
    assert database_facts(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT MAX(version) FROM agent_migrations").fetchone()[0] == (
            27 if point == "before_commit" else 30
        )
    store = SQLiteSessionStore(path)
    await store.initialize()
    assert database_facts(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT publication_epoch,publication_policy FROM agent_artifacts"
        ).fetchall() == [(None, None)]
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    args = UUID(record["thread_id"]), record["scope"], UUID(record["ref"]["artifact_id"])
    assert (await SQLiteArtifactStore(store).read(*args)).text
    with protected() as scope:
        with pytest.raises(KernelError) as denied:
            await SQLiteArtifactStore(store, public_output_protection=scope).read(*args)
        assert denied.value.code == "artifact_publication_unproven"
    assert database_facts(path) == before
