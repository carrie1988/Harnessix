from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

from harnessix.bootstrap import build_service
from harnessix.domain.models import ActionStatus, TraceContext
from harnessix.settings import Settings
from harnessix.storage import SQLiteEffectJournal
from harnessix.worker import ActionWorker
from tests.helpers import RecordingObservability, action_request


async def test_trace_context_is_durable_across_api_and_worker(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "observability.db",
        demo_database_path=tmp_path / "external.db",
        execution_mode="queued",
        lease_seconds=2,
        worker_heartbeat_seconds=0.2,
    )
    parent = TraceContext(traceparent="00-11111111111111111111111111111111-2222222222222222-01")
    api_observability = RecordingObservability(parent)
    api_service = build_service(settings, observability=api_observability)
    await api_service.initialize()
    request = action_request("system.echo", {"message": "trace"})
    try:
        ready = await api_service.submit(request)
    finally:
        await api_service.close()

    worker_observability = RecordingObservability(parent)
    worker_service = build_service(
        settings,
        worker_id="trace-worker",
        observability=worker_observability,
    )
    await worker_service.initialize()
    try:
        completed = await ActionWorker(worker_service, heartbeat_seconds=0.2).run_once()
    finally:
        await worker_service.close()

    assert ready.status is ActionStatus.READY
    assert ready.trace_context == parent
    assert completed is not None
    assert completed.status is ActionStatus.SUCCEEDED
    assert ("harnessix.worker.consume", "consumer", parent) in worker_observability.spans
    assert all(
        "action_id" not in attributes and "tenant_id" not in attributes
        for _, _, _, attributes in api_observability.metrics + worker_observability.metrics
    )
    completed_metrics = [
        metric
        for metric in worker_observability.metrics
        if metric[1] == "harnessix.actions.completed"
    ]
    assert len(completed_metrics) == 1


async def test_duplicate_submission_does_not_double_count_completion(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "duplicates.db",
        demo_database_path=tmp_path / "external.db",
    )
    observability = RecordingObservability()
    service = build_service(settings, observability=observability)
    await service.initialize()
    request = action_request("system.echo", {"message": "once"})
    try:
        await service.submit(request)
        await service.submit(request)
    finally:
        await service.close()

    completed_metrics = [
        metric for metric in observability.metrics if metric[1] == "harnessix.actions.completed"
    ]
    assert len(completed_metrics) == 1


async def test_sqlite_applies_observability_migration_to_existing_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "m1-existing.db"
    migration = files("harnessix.storage.migrations").joinpath("0001_initial.sql").read_text()
    with sqlite3.connect(database_path) as connection:
        connection.executescript(migration)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
            ("2026-09-01T00:00:00+00:00",),
        )

    journal = SQLiteEffectJournal(database_path)
    await journal.initialize()
    await journal.initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(actions)").fetchall()}
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}

    assert "trace_context_json" in columns
    assert versions == {1, 2}
