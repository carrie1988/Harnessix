"""过期Artifact正文的自动回收：启动一次、定期一轮，失败不影响宿主，坏行不卡住游标。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import timedelta
from uuid import UUID, uuid4

from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import CollectionReport
from harnessix.artifacts.reclaim import ArtifactReclaimer, start_artifact_reclaim
from tests.artifacts.helpers import exercise


def _states(path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute("SELECT state FROM agent_artifacts")]


class _Recorder:
    """按调用次序返回预设报告或抛错的回收端口。"""

    def __init__(self, reports: list[CollectionReport | Exception]) -> None:
        self.reports = reports
        self.cursors: list[UUID | None] = []

    async def collect(self, *, limit: int = 100, after: UUID | None = None) -> CollectionReport:
        self.cursors.append(after)
        report = self.reports.pop(0) if self.reports else _report(None)
        if isinstance(report, Exception):
            raise report
        return report


def _report(next_after: UUID | None) -> CollectionReport:
    return CollectionReport(1, 1, 0, artifact_sqlite.utc_now(), next_after)


async def test_periodic_sweep_reclaims_bodies_that_expire_while_open(tmp_path, monkeypatch):
    store, artifacts, _, _, _ = await exercise(tmp_path, count=1)
    reclaimer = ArtifactReclaimer(artifacts, interval_seconds=0.01)
    await reclaimer.start()
    try:
        assert _states(store.path) == ["published"]
        future = artifact_sqlite.utc_now() + timedelta(days=2)
        monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
        for _ in range(200):
            if _states(store.path) == ["expired"]:
                break
            await asyncio.sleep(0.01)
        assert _states(store.path) == ["expired"]
    finally:
        await reclaimer.stop()


async def test_failed_sweep_is_logged_and_retried_without_raising(caplog):
    store = _Recorder([RuntimeError("database is locked"), _report(None)])
    reclaimer = ArtifactReclaimer(store, interval_seconds=0.01)
    with caplog.at_level(logging.WARNING, logger="harnessix.artifacts.reclaim"):
        await reclaimer.start()
        for _ in range(200):
            if len(store.cursors) >= 2:
                break
            await asyncio.sleep(0.01)
        await reclaimer.stop()
    assert len(store.cursors) >= 2
    assert "RuntimeError" in caplog.text and "database is locked" not in caplog.text


async def test_startup_backlog_beyond_budget_continues_in_background():
    cursor = uuid4()
    store = _Recorder([_report(cursor), _report(cursor), _report(None)])
    reclaimer = ArtifactReclaimer(store, interval_seconds=3600, startup_budget_seconds=0)
    await reclaimer.start()
    assert store.cursors == [None]
    for _ in range(200):
        if len(store.cursors) >= 3:
            break
        await asyncio.sleep(0.01)
    await reclaimer.stop()
    assert store.cursors == [None, None, cursor]


async def test_stop_cancels_the_periodic_task():
    store = _Recorder([])
    reclaimer = ArtifactReclaimer(store, interval_seconds=3600)
    await reclaimer.start()
    task = reclaimer._task
    await reclaimer.stop()
    assert task is not None and task.cancelled()
    assert store.cursors == [None]


async def test_only_stores_that_can_collect_are_scheduled():
    assert await start_artifact_reclaim(object()) is None


async def test_unverifiable_row_is_kept_and_does_not_block_the_cursor(tmp_path, monkeypatch):
    store, artifacts, _, _, _ = await exercise(tmp_path, count=1)
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM agent_artifacts").fetchone())
        # 排在真实行之前、没有Session结果引用的到期行：旧实现在这里中止整批回收。
        orphan = "00000000-0000-4000-8000-000000000000"
        manifest = json.loads(row["manifest_json"])
        manifest["artifact_id"] = orphan
        db.execute(
            "INSERT INTO agent_artifacts (artifact_id,thread_id,turn_id,call_id,workspace_scope,"
            "manifest_json,size_bytes,expires_at,state,body,purpose,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,'published',?,?,?)",
            (
                orphan,
                row["thread_id"],
                row["turn_id"],
                str(uuid4()),
                row["workspace_scope"],
                json.dumps(manifest),
                row["size_bytes"],
                row["expires_at"],
                row["body"],
                row["purpose"],
                row["created_at"],
            ),
        )
    future = artifact_sqlite.utc_now() + timedelta(days=2)
    monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
    report = await artifacts.collect()
    assert (report.examined, report.expired, report.protected) == (2, 1, 1)
    with sqlite3.connect(store.path) as db:
        kept = db.execute(
            "SELECT state FROM agent_artifacts WHERE artifact_id = ?", (orphan,)
        ).fetchone()
    assert kept == ("published",)
