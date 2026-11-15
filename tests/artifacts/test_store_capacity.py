"""工作区Artifact总量：过期正文与回收后的Tombstone不能让此后每次归档都中断Turn。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

from harnessix.agent.models import TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import ArtifactPolicy
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import results, step


def _workspace(tmp_path, policy=None, *, matches=1):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle 中文\n" * matches, encoding="utf-8")
    store = SQLiteSessionStore(tmp_path / "s.db")
    return root, store, SQLiteArtifactStore(store, policy=policy)


async def _archive_turn(root, store, artifacts, *, steps=1):
    """新Thread里每步一次产生归档的搜索，返回结束后的Turn。"""
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            ScriptedProvider([step() for _ in range(steps)] + [answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            return await runtime.run_turn(thread.thread_id, "归档", request_id=str(uuid4()))


def _seed_tombstones(path, count):
    """复制唯一一行真实Artifact为已回收的Tombstone，模拟同一工作区里早先会话留下的大量归档。"""
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM agent_artifacts").fetchone())
        expires = "2000-01-01T00:00:00+00:00"
        for _ in range(count):
            artifact_id = str(uuid4())
            manifest = json.loads(row["manifest_json"])
            manifest.update(artifact_id=artifact_id, expires_at=expires)
            db.execute(
                "INSERT INTO agent_artifacts (artifact_id,thread_id,turn_id,call_id,"
                "workspace_scope,manifest_json,size_bytes,expires_at,state,body,purpose,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    row["thread_id"],
                    row["turn_id"],
                    str(uuid4()),
                    row["workspace_scope"],
                    json.dumps(manifest),
                    row["size_bytes"],
                    expires,
                    "expired",
                    None,
                    row["purpose"],
                    row["created_at"],
                ),
            )


async def test_collected_tombstones_do_not_block_later_archives(tmp_path, monkeypatch):
    # 回收只清空正文、保留Tombstone；Tombstone仍计入份数上限时，回收之后照样中断。
    root, store, artifacts = _workspace(tmp_path, ArtifactPolicy(max_manifests=2))
    first = await _archive_turn(root, store, artifacts, steps=2)
    assert first.status == TurnStatus.COMPLETED, first.error
    future = artifact_sqlite.utc_now() + timedelta(days=2)
    monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
    assert (await artifacts.collect()).expired == 2
    later = await _archive_turn(root, store, artifacts)
    assert later.status == TurnStatus.COMPLETED, later.error
    assert "artifact" in results(later)[0].output


async def test_default_manifest_quota_survives_ten_thousand_tombstones(tmp_path):
    # 默认10000份：同一工作区累计用满后，此后每次归档都中断Turn，只能删状态目录恢复。
    root, store, artifacts = _workspace(tmp_path)
    first = await _archive_turn(root, store, artifacts)
    assert first.status == TurnStatus.COMPLETED, first.error
    _seed_tombstones(store.path, artifacts.policy.max_manifests)
    later = await _archive_turn(root, store, artifacts)
    assert later.status == TurnStatus.COMPLETED, later.error
    assert "artifact" in results(later)[0].output


async def test_default_live_bytes_reclaims_expired_bodies(tmp_path, monkeypatch):
    # 默认32 MiB：过期正文从不自动回收，累计满32 MiB后此后每次归档都中断Turn。
    root, store, artifacts = _workspace(tmp_path, matches=5000)
    for _ in range(4):
        turn = await _archive_turn(root, store, artifacts, steps=10)
        assert turn.status == TurnStatus.COMPLETED, turn.error
    with sqlite3.connect(store.path) as db:
        live, largest = db.execute(
            "SELECT SUM(length(body)), MAX(size_bytes) FROM agent_artifacts"
        ).fetchone()
    assert live + largest > artifacts.policy.max_live_bytes
    future = artifact_sqlite.utc_now() + timedelta(days=2)
    monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
    later = await _archive_turn(root, store, artifacts)
    assert later.status == TurnStatus.COMPLETED, later.error
    assert "artifact" in results(later)[0].output
    with sqlite3.connect(store.path) as db:
        live = db.execute("SELECT SUM(length(body)) FROM agent_artifacts").fetchone()[0]
    assert live == largest
