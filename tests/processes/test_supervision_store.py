from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _lease(process_id: int = 1) -> ProcessLease:
    return ProcessLease(
        process_id=UUID(f"00000000-0000-4000-8000-{process_id:012d}"),
        plan_id=UUID("00000000-0000-4000-8000-000000000099"),
        plan_fingerprint="a" * 64,
        process_spec_digest="b" * 64,
        capability_digest="c" * 64,
        lifecycle="background",
        state="prepared",
        sequence=0,
        owner_token="d" * 64,
        deadline=NOW + timedelta(minutes=1),
    )


def _running(prepared: ProcessLease) -> tuple[ProcessLease, ProcessLease]:
    starting = prepared.model_copy(update={"state": "starting", "sequence": 1})
    running = starting.model_copy(
        update={
            "state": "running",
            "sequence": 2,
            "owner_identity": "e" * 64,
            "pid": 123,
            "started_at": NOW,
        }
    )
    return starting, ProcessLease.model_validate_json(running.model_dump_json())


def test_process_lease_store_is_append_only_durable_and_cas_guarded(tmp_path: Path) -> None:
    path = tmp_path / "private/process.db"
    prepared = _lease()
    starting, running = _running(prepared)
    with SQLiteProcessLeaseStore(path) as store:
        store.create(prepared)
        store.create(prepared)
        store.transition(prepared, starting)
        store.transition(starting, running)
        assert store.active() == (running,)
        with pytest.raises(KernelError) as stale:
            store.transition(starting, running)
        assert stale.value.code == "process_lease_stale"
    with SQLiteProcessLeaseStore(path) as reopened:
        assert reopened.load(prepared.process_id) == running
        rows = reopened._db.execute(  # noqa: SLF001 - 验证append-only事件数量
            "SELECT sequence, state FROM process_lease_events ORDER BY sequence"
        ).fetchall()
    assert rows == [(0, "prepared"), (1, "starting"), (2, "running")]


def test_process_lease_store_rejects_illegal_transition_and_binding_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process.db"
    prepared = _lease()
    _, running = _running(prepared)
    with SQLiteProcessLeaseStore(path) as store:
        store.create(prepared)
        with pytest.raises(KernelError) as skipped:
            store.transition(prepared, running)
        assert skipped.value.code == "process_transition_invalid"
        changed = prepared.model_copy(
            update={"state": "starting", "sequence": 1, "plan_fingerprint": "f" * 64}
        )
        with pytest.raises(KernelError) as binding:
            store.transition(prepared, changed)
        assert binding.value.code == "process_transition_invalid"


def test_process_lease_store_fails_closed_on_unknown_version_and_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process.db"
    database = sqlite3.connect(path)
    database.execute(
        "CREATE TABLE process_store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO process_store_metadata VALUES ('schema_version', '2')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteProcessLeaseStore(path)
    assert version.value.code == "process_store_version"

    path.unlink()
    prepared = _lease()
    with SQLiteProcessLeaseStore(path) as store:
        store.create(prepared)
        store._db.execute(  # noqa: SLF001 - 故障注入验证损坏记录失败关闭
            "UPDATE process_leases SET payload = '{}' WHERE process_id = ?",
            (str(prepared.process_id),),
        )
        with pytest.raises(KernelError) as corrupt:
            store.load(prepared.process_id)
    assert corrupt.value.code == "process_store_corrupt"


@pytest.mark.parametrize("change", ["state", "event"])
def test_process_lease_store_rejects_index_or_event_divergence(tmp_path: Path, change: str) -> None:
    path = tmp_path / "process.db"
    prepared = _lease()
    with SQLiteProcessLeaseStore(path) as store:
        store.create(prepared)
        if change == "state":
            store._db.execute(  # noqa: SLF001 - 故障注入验证索引漂移
                "UPDATE process_leases SET state = 'running' WHERE process_id = ?",
                (str(prepared.process_id),),
            )
        else:
            store._db.execute(  # noqa: SLF001 - 故障注入验证事件丢失
                "DELETE FROM process_lease_events WHERE process_id = ?",
                (str(prepared.process_id),),
            )
        with pytest.raises(KernelError) as corrupt:
            store.load(prepared.process_id)
    assert corrupt.value.code == "process_store_corrupt"
