from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import utc_now
from harnessix.models.scripted import FakeProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.maintenance import SQLiteStoreMaintenance
from harnessix.session.maintenance_contracts import RetentionPolicy
from tests.artifacts.helpers import exercise, results


async def _archived_fixture(tmp_path: Path):
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    async with AgentRuntime(store, FakeProvider()) as runtime:
        archived = await runtime.archive_thread(thread.thread_id, reason="容量测试")
    artifact_id = UUID(results(turn)[0].output["artifact"]["artifact_id"])
    return store, artifacts, scope, archived, artifact_id


async def test_capacity_report_is_complete_and_low_sensitive(tmp_path: Path) -> None:
    store, _, _, thread, _ = await _archived_fixture(tmp_path)
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()
    await requests.claim(client, "done", "thread/archive", {"prompt": "SECRET-CANARY"})
    await requests.complete(client, "done", {"threadId": str(thread.thread_id)})

    report = await SQLiteStoreMaintenance(store).snapshot()

    assert report.schema_version == 27
    assert tuple(item.store_kind for item in report.stores) == (
        "session",
        "protocol_request",
        "artifact",
    )
    assert report.stores[0].logical_rows_by_kind["threads"] == 1
    assert report.stores[0].terminal_rows == 1
    assert report.stores[1].terminal_rows == 1
    assert report.stores[2].artifact_body_bytes > 0
    encoded = report.model_dump_json()
    assert str(tmp_path) not in encoded
    assert str(thread.thread_id) not in encoded
    assert "SECRET-CANARY" not in encoded


async def test_plan_requires_runtime_owner_and_detects_tamper(tmp_path: Path) -> None:
    store, _, _, _, _ = await _archived_fixture(tmp_path)
    maintenance = SQLiteStoreMaintenance(store)
    policy = RetentionPolicy(cutoff=utc_now() + timedelta(days=2))

    with pytest.raises(KernelError) as caught:
        await maintenance.plan(policy)
    assert caught.value.code == "maintenance_runtime_required"

    async with store.runtime_owner():
        plan = await maintenance.plan(policy)
        with sqlite3.connect(store.path) as database:
            database.execute(
                "UPDATE store_maintenance_plans SET payload_sha256=? WHERE plan_id=?",
                ("0" * 64, str(plan.plan_id)),
            )
        with pytest.raises(KernelError) as caught:
            await maintenance.load_plan(plan.plan_id)
        assert caught.value.code == "maintenance_corrupt"


async def test_pending_protocol_conservatively_protects_business_state(tmp_path: Path) -> None:
    store, _, _, thread, _ = await _archived_fixture(tmp_path)
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()
    await requests.claim(client, "done", "thread/archive", {})
    await requests.complete(client, "done", {"threadId": str(thread.thread_id)})
    await requests.claim(client, "pending", "turn/start", {})

    async with store.runtime_owner():
        maintenance = SQLiteStoreMaintenance(store)
        plan = await maintenance.plan(RetentionPolicy(cutoff=utc_now() + timedelta(days=2)))
        assert plan.candidates_by_kind == {"protocol_request": 1}
        assert plan.protected_by_reason["accepted_protocol_global"] >= 2
        result = await maintenance.execute(
            plan.plan_id,
            backup_path=tmp_path / "pending-backup.db",
            batch_size=1,
        )

    assert result.progress.state == "completed"
    assert result.progress.applied_items == 1
    assert await store.get_thread(thread.thread_id) == thread
    with sqlite3.connect(store.path) as database:
        assert database.execute("SELECT state FROM protocol_requests").fetchall() == [("accepted",)]
        assert database.execute("SELECT state FROM agent_artifacts").fetchone() == ("published",)


async def test_backup_creation_crash_reuses_verified_plan_backup(tmp_path: Path) -> None:
    store, _, _, _, _ = await _archived_fixture(tmp_path)
    backup = tmp_path / "created-before-progress.db"
    failed_once = False

    def fail_after_backup(point: str) -> None:
        nonlocal failed_once
        if point == "maintenance.after_backup_created" and not failed_once:
            failed_once = True
            raise RuntimeError("注入备份后退出")

    async with store.runtime_owner():
        failing = SQLiteStoreMaintenance(store, fault=fail_after_backup)
        plan = await failing.plan(
            RetentionPolicy(
                cutoff=utc_now() - timedelta(days=1),
                expire_artifact_bodies=False,
                delete_archived_threads=False,
                delete_terminal_protocol_requests=False,
            )
        )
        with pytest.raises(RuntimeError, match="注入备份后退出"):
            await failing.execute(plan.plan_id, backup_path=backup)
        assert (await failing.progress(plan.plan_id)).state == "planned"

        result = await SQLiteStoreMaintenance(store).execute(plan.plan_id, backup_path=backup)
        assert result.progress.state == "completed"
        assert result.progress.total_items == 0


async def test_crash_resume_and_backup_restore_are_atomic(tmp_path: Path) -> None:
    store, _, _, thread, artifact_id = await _archived_fixture(tmp_path)
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()
    await requests.claim(client, "done", "thread/archive", {})
    await requests.complete(client, "done", {"threadId": str(thread.thread_id)})
    backup = tmp_path / "maintenance-backup.db"
    failed_once = False

    def fail_after_first_batch(point: str) -> None:
        nonlocal failed_once
        if point == "maintenance.after_batch_commit" and not failed_once:
            failed_once = True
            raise RuntimeError("注入维护进程退出")

    async with store.runtime_owner():
        policy = RetentionPolicy(cutoff=utc_now() + timedelta(days=2), max_items=10)
        failing = SQLiteStoreMaintenance(store, fault=fail_after_first_batch)
        plan = await failing.plan(policy)
        assert plan.candidates_by_kind == {
            "artifact_body": 1,
            "session_thread": 1,
            "protocol_request": 1,
        }
        with pytest.raises(RuntimeError, match="注入维护进程退出"):
            await failing.execute(plan.plan_id, backup_path=backup, batch_size=1)
        progress = await failing.progress(plan.plan_id)
        assert progress.state == "running" and progress.next_ordinal == 1

        resumed = SQLiteStoreMaintenance(store)
        result = await resumed.execute(plan.plan_id, backup_path=backup, batch_size=1)
        assert result.progress.state == "completed"
        assert result.progress.applied_items == 3
        assert result.progress.skipped_items == 0
        assert result.after.stores[0].logical_rows_by_kind["threads"] == 0
        assert result.after.stores[1].logical_rows_by_kind["requests"] == 0
        assert result.after.stores[2].logical_rows_by_kind["manifests"] == 0
        with pytest.raises(KernelError) as caught:
            await store.get_thread(thread.thread_id)
        assert caught.value.code == "thread_not_found"

        restored = await resumed.restore(backup)
        assert restored.backup_sha256 == result.progress.backup_sha256
        assert restored.capacity.stores[0].logical_rows_by_kind["threads"] == 1

    assert await store.get_thread(thread.thread_id) == thread
    with sqlite3.connect(store.path) as database:
        assert database.execute(
            "SELECT state FROM agent_artifacts WHERE artifact_id=?", (str(artifact_id),)
        ).fetchone() == ("published",)
        assert database.execute("SELECT COUNT(*) FROM protocol_requests").fetchone() == (1,)


async def test_changed_candidate_is_skipped_not_deleted(tmp_path: Path) -> None:
    store, _, _, _, _ = await _archived_fixture(tmp_path)
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()
    await requests.claim(client, "done", "thread/archive", {})
    await requests.complete(client, "done", {"ok": True})

    async with store.runtime_owner():
        maintenance = SQLiteStoreMaintenance(store)
        plan = await maintenance.plan(
            RetentionPolicy(
                cutoff=utc_now() + timedelta(days=2),
                expire_artifact_bodies=False,
                delete_archived_threads=False,
            )
        )
        with sqlite3.connect(store.path) as database:
            database.execute(
                "UPDATE protocol_requests SET updated_at=? WHERE client_instance_id=?",
                ((utc_now() + timedelta(days=1)).isoformat(), str(client)),
            )
        result = await maintenance.execute(
            plan.plan_id,
            backup_path=tmp_path / "changed-backup.db",
        )

    assert result.progress.applied_items == 0
    assert result.progress.skipped_items == 1
    assert await requests.get(client, "done") is not None


async def test_new_pending_request_after_plan_protects_artifact_and_session(tmp_path: Path) -> None:
    store, _, _, thread, _ = await _archived_fixture(tmp_path)
    requests = SQLiteProtocolRequestStore(store.path)
    client = uuid4()

    async with store.runtime_owner():
        maintenance = SQLiteStoreMaintenance(store)
        plan = await maintenance.plan(
            RetentionPolicy(
                cutoff=utc_now() + timedelta(days=2),
                delete_terminal_protocol_requests=False,
            )
        )
        assert plan.candidates_by_kind == {"artifact_body": 1, "session_thread": 1}
        await requests.claim(client, "late-pending", "turn/start", {})
        result = await maintenance.execute(
            plan.plan_id,
            backup_path=tmp_path / "late-pending-backup.db",
        )

    assert result.progress.applied_items == 0
    assert result.progress.skipped_items == 2
    assert await store.get_thread(thread.thread_id) == thread
    with sqlite3.connect(store.path) as database:
        assert database.execute("SELECT state FROM agent_artifacts").fetchone() == ("published",)
