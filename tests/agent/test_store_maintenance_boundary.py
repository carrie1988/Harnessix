"""真实SQLite维护拒绝、候选预检与受管工作线程；不构造模型请求。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import utc_now
from harnessix.models.scripted import FakeProvider
from harnessix.session.capacity import capacity_report
from harnessix.session.maintenance import SQLiteStoreMaintenance
from harnessix.session.maintenance_backup import create_or_reuse_backup, file_sha256
from harnessix.session.maintenance_contracts import RetentionPolicy
from harnessix.session.maintenance_execution import run_batches
from harnessix.session.maintenance_io import MaintenanceIOControl, run_maintenance_io
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from tests.agent.test_publication import protected


@asynccontextmanager
async def _keyed(tmp_path):
    with protected() as scope:
        binding = SessionPublicationBinding(uuid4(), uuid4(), bytes(range(32)), scope)
        try:
            store = SQLiteSessionStore(tmp_path / "authenticated.db", publication=binding)
            async with AgentRuntime(
                store, FakeProvider(), public_output_protection=scope
            ) as runtime:
                thread = await runtime.create_thread(tmp_path.as_posix())
                thread = await runtime.archive_thread(thread.thread_id, reason="维护边界")
            yield store, thread
        finally:
            binding.close()


async def _legacy(path):
    store = SQLiteSessionStore(path)
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(path.parent.as_posix())
    return store, thread


def _backup(store, destination):
    with (
        closing(sqlite3.connect(store.path)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)


def _sql(path, query, values=()):
    with closing(sqlite3.connect(path)) as database:
        database.execute(query, values)
        database.commit()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _no_staging(path):
    assert not list(path.parent.glob(f".{path.name}.restore-*.tmp*"))


async def test_authenticated_capacity_uses_original_projection_seal(tmp_path):
    async with _keyed(tmp_path) as (store, thread):
        report = await SQLiteStoreMaintenance(store).snapshot()
        assert report.stores[0].logical_rows_by_kind["threads"] == 1
        assert report.stores[0].terminal_rows == 1
        assert str(thread.thread_id) not in report.model_dump_json()


@pytest.mark.parametrize("body", ["valid_json", "invalid_json"])
async def test_forged_projection_with_recomputed_plain_hash_is_rejected(tmp_path, body):
    async with _keyed(tmp_path) as (store, thread):
        with closing(sqlite3.connect(store.path)) as database:
            raw = database.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
        if body == "valid_json":
            value = json.loads(raw)
            value["archive"]["reason"] = "伪造保留理由"
            encoded = json.dumps(value, ensure_ascii=False)
        else:
            # 去掉JSON表达式索引后才能模拟文件写入者留下的非法原字节。
            _sql(store.path, "DROP INDEX agent_threads_archive_list_idx")
            encoded = "{not-json"
        _sql(
            store.path,
            "UPDATE agent_threads SET snapshot_json=?,snapshot_sha256=? WHERE thread_id=?",
            (encoded, hashlib.sha256(encoded.encode()).hexdigest(), str(thread.thread_id)),
        )
        before = _digest(store.path)
        with pytest.raises(KernelError) as refused:
            await SQLiteStoreMaintenance(store).snapshot()
        assert refused.value.code == "publication_history_unproven"
        assert _digest(store.path) == before


async def test_capacity_without_binding_cannot_downgrade_authenticated_store(tmp_path):
    async with _keyed(tmp_path) as (store, _):
        async with store._connection() as database:
            await database.execute("BEGIN")
            with pytest.raises(KernelError) as refused:
                await capacity_report(database, store.path)
            assert refused.value.code == "publication_key_unavailable"


@pytest.mark.parametrize("operation", ["plan", "load_plan", "progress", "execute", "restore"])
async def test_authenticated_unproven_maintenance_is_refused_without_writes(tmp_path, operation):
    async with _keyed(tmp_path) as (store, _):
        maintenance = SQLiteStoreMaintenance(store)
        backup = tmp_path / "never-published.db"
        before = _digest(store.path)
        async with store.runtime_owner():
            with pytest.raises(KernelError) as refused:
                if operation == "plan":
                    await maintenance.plan(RetentionPolicy(cutoff=utc_now() + timedelta(days=1)))
                elif operation == "load_plan":
                    await maintenance.load_plan(uuid4())
                elif operation == "progress":
                    await maintenance.progress(uuid4())
                elif operation == "execute":
                    await maintenance.execute(uuid4(), backup_path=backup)
                else:
                    await maintenance.restore(backup)
            assert refused.value.code == "maintenance_authenticated_unavailable"
        assert _digest(store.path) == before
        assert not backup.exists()
        with closing(sqlite3.connect(store.path)) as database:
            assert (
                database.execute("SELECT COUNT(*) FROM store_maintenance_plans").fetchone()[0] == 0
            )
            assert (
                database.execute("SELECT COUNT(*) FROM agent_event_publications").fetchone()[0] > 0
            )


async def test_direct_batch_runner_keeps_authenticated_maintenance_guard(tmp_path):
    async with _keyed(tmp_path) as (store, _):
        async with store.runtime_owner():
            with pytest.raises(KernelError) as refused:
                await run_batches(
                    store, uuid4(), batch_size=1, owner=object(), fault=lambda _: None
                )
            assert refused.value.code == "maintenance_authenticated_unavailable"


@pytest.mark.parametrize("keyed", [False, True])
async def test_orphan_event_cannot_disappear_from_capacity_report(tmp_path, keyed):
    async def reject(store):
        _sql(store.path, "DELETE FROM agent_threads")
        with pytest.raises(KernelError) as refused:
            await SQLiteStoreMaintenance(store).snapshot()
        assert refused.value.code == "projection_corrupt"

    if keyed:
        async with _keyed(tmp_path) as (store, _):
            await reject(store)
    else:
        store, _ = await _legacy(tmp_path / "legacy.db")
        await reject(store)


async def test_keyed_backup_is_refused_before_overwriting_legacy_target(tmp_path):
    target, thread = await _legacy(tmp_path / "target.db")
    async with _keyed(tmp_path) as (source, _):
        backup = tmp_path / "keyed-backup.db"
        _backup(source, backup)
    before = _digest(target.path)
    async with target.runtime_owner():
        with pytest.raises(KernelError) as refused:
            await SQLiteStoreMaintenance(target).restore(backup)
        assert refused.value.code == "publication_key_unavailable"
    assert _digest(target.path) == before
    assert await target.get_thread(thread.thread_id) == thread
    _no_staging(target.path)


async def test_unkeyed_adapter_cannot_overwrite_keyed_target(tmp_path):
    source, _ = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "legacy-backup.db"
    _backup(source, backup)
    async with _keyed(tmp_path) as (target, thread):
        adapter = SQLiteSessionStore(target.path)
        before = _digest(target.path)
        async with adapter.runtime_owner():
            with pytest.raises(KernelError) as refused:
                await SQLiteStoreMaintenance(adapter).restore(backup)
            assert refused.value.code == "publication_key_unavailable"
        assert _digest(target.path) == before
        assert await target.get_thread(thread.thread_id) == thread


@pytest.mark.parametrize(
    ("query", "values", "code"),
    [
        (
            "UPDATE agent_migrations SET checksum=? WHERE version=1",
            ("0" * 64,),
            "migration_changed",
        ),
        ("INSERT INTO agent_migrations VALUES (32,?)", ("0" * 64,), "schema_too_new"),
        ("DELETE FROM agent_threads", (), "projection_missing"),
        ("UPDATE agent_threads SET snapshot_sha256=?", ("0" * 64,), "projection_corrupt"),
    ],
)
async def test_invalid_restore_candidate_preserves_current_database(tmp_path, query, values, code):
    target, thread = await _legacy(tmp_path / "target.db")
    source, _ = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "invalid-backup.db"
    _backup(source, backup)
    _sql(backup, query, values)
    before, backup_before = _digest(target.path), _digest(backup)
    async with target.runtime_owner():
        with pytest.raises(KernelError) as refused:
            await SQLiteStoreMaintenance(target).restore(backup)
        assert refused.value.code == code
    assert _digest(target.path) == before
    assert _digest(backup) == backup_before
    assert await target.get_thread(thread.thread_id) == thread
    _no_staging(target.path)


async def test_valid_restore_handles_uri_reserved_characters(tmp_path):
    target, _ = await _legacy(tmp_path / "target.db")
    source, expected = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "backup # & %.db"
    _backup(source, backup)
    async with target.runtime_owner():
        report = await SQLiteStoreMaintenance(target).restore(backup)
    assert report.backup_sha256 == _digest(backup)
    assert await target.get_thread(expected.thread_id) == expected
    _no_staging(target.path)


async def test_restore_cancellation_settles_original_worker_and_cleans_stage(tmp_path):
    target, thread = await _legacy(tmp_path / "target.db")
    source, _ = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    _backup(source, backup)
    entered, release = Event(), Event()

    def pause(point):
        if point == "maintenance.restore_after_validation":
            entered.set()
            assert release.wait(5)

    before = _digest(target.path)
    async with target.runtime_owner():
        task = asyncio.create_task(SQLiteStoreMaintenance(target, fault=pause).restore(backup))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert target._runtime_owner_token is not None
    assert _digest(target.path) == before
    assert await target.get_thread(thread.thread_id) == thread
    _no_staging(target.path)


@pytest.mark.parametrize("reason", ["fault", "owner_closed"])
async def test_validated_restore_refuses_publication_after_failure_or_owner_loss(tmp_path, reason):
    target, thread = await _legacy(tmp_path / "target.db")
    source, _ = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    _backup(source, backup)

    def fail(point):
        if point == "maintenance.restore_after_validation":
            if reason == "fault":
                raise RuntimeError("validated-restore-fault")
            target._runtime_owner_token = None

    before = _digest(target.path)
    async with target.runtime_owner():
        if reason == "fault":
            with pytest.raises(RuntimeError, match="validated-restore-fault"):
                await SQLiteStoreMaintenance(target, fault=fail).restore(backup)
        else:
            with pytest.raises(KernelError) as refused:
                await SQLiteStoreMaintenance(target, fault=fail).restore(backup)
            assert refused.value.code == "maintenance_runtime_required"
    assert _digest(target.path) == before
    assert await target.get_thread(thread.thread_id) == thread
    _no_staging(target.path)


async def test_actual_wal_reader_prevents_restore_publication(tmp_path, monkeypatch):
    import harnessix.session.maintenance_backup as backup_module

    target, thread = await _legacy(tmp_path / "target.db")
    source, _ = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    _backup(source, backup)
    reader = sqlite3.connect(target.path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM agent_threads").fetchone()
    # 同值UPDATE可能不生成WAL帧；通过真实Runtime新增线程形成被旧读视图阻塞的提交。
    async with AgentRuntime(target, FakeProvider()) as runtime:
        await runtime.create_thread(tmp_path.as_posix())
    assert reader.execute("SELECT COUNT(*) FROM agent_threads").fetchone()[0] == 1
    assert (tmp_path / "target.db-wal").stat().st_size > 32
    original_connect = sqlite3.connect

    def bounded_connect(*args, **kwargs):
        kwargs["timeout"] = 0.05
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(backup_module.sqlite3, "connect", bounded_connect)
    try:
        async with target.runtime_owner():
            with pytest.raises(KernelError) as refused:
                await SQLiteStoreMaintenance(target).restore(backup)
            assert refused.value.code == "maintenance_restore_busy"
        assert await target.get_thread(thread.thread_id) == thread
        _no_staging(target.path)
    finally:
        reader.rollback()
        reader.close()


async def test_post_publication_fault_does_not_blindly_restore_again(tmp_path):
    target, original = await _legacy(tmp_path / "target.db")
    source, restored = await _legacy(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    _backup(source, backup)
    calls = 0

    def fail(point):
        nonlocal calls
        if point == "maintenance.restore_after_publish":
            calls += 1
            raise RuntimeError("restore-confirmation-lost")

    async with target.runtime_owner():
        with pytest.raises(RuntimeError, match="restore-confirmation-lost"):
            await SQLiteStoreMaintenance(target, fault=fail).restore(backup)
    assert calls == 1
    assert await target.get_thread(restored.thread_id) == restored
    with pytest.raises(KernelError) as refused:
        await target.get_thread(original.thread_id)
    assert refused.value.code == "thread_not_found"
    _no_staging(target.path)


async def test_owned_worker_deadline_and_cancel_are_not_retried(tmp_path):
    calls = 0

    def operation(control):
        nonlocal calls
        calls += 1
        control.checkpoint()
        (tmp_path / "unexpected").write_text("written")

    with pytest.raises(KernelError) as refused:
        await run_maintenance_io(operation, budget_seconds=0)
    assert refused.value.code == "maintenance_io_timeout"
    assert calls == 1
    assert not (tmp_path / "unexpected").exists()
    control = MaintenanceIOControl()
    control.cancel()
    assert control.interrupt() == 1


def test_backup_size_bound_precedes_unbounded_hashing(tmp_path, monkeypatch):
    monkeypatch.setattr("harnessix.session.maintenance_backup.MAX_BACKUP_BYTES", 8)
    path = tmp_path / "large.db"
    path.write_bytes(b"x" * 9)
    with pytest.raises(KernelError) as refused:
        file_sha256(path)
    assert refused.value.code == "maintenance_backup_limit"


async def test_racing_backup_publication_never_overwrites_existing_file(tmp_path, monkeypatch):
    import harnessix.session.maintenance_backup as backup_module

    store, _ = await _legacy(tmp_path / "session.db")
    destination = tmp_path / "racing.db"
    async with store.runtime_owner():
        plan = await SQLiteStoreMaintenance(store).plan(RetentionPolicy(cutoff=utc_now()))
    real_link = backup_module.os.link

    def race(source, target):
        destination.write_bytes(b"competitor")
        real_link(source, target)

    monkeypatch.setattr(backup_module.os, "link", race)
    with pytest.raises(KernelError) as refused:
        create_or_reuse_backup(
            store.path,
            destination,
            plan.plan_id,
            hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
        )
    assert refused.value.code == "storage_unavailable"
    assert destination.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".racing.db.backup-*.tmp"))
