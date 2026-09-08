from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import transition_transaction_record
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore


def _prepared(root: Path):
    target = root / "target.txt"
    target.write_bytes(b"before\n")
    target.chmod(0o644)
    return prepare_workspace_transaction(
        root,
        {"target.txt": DesiredWorkspaceFile(b"after\n", 0o644)},
        request_id="request-1",
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )


def test_store_persists_blobs_events_and_progressed_idempotency(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepared = _prepared(workspace)
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(prepared)
        started = transition_transaction_record(
            record,
            state="publishing",
            cursor=0,
            now=datetime(2026, 9, 8, 1, tzinfo=UTC),
        )
        store.transition(record, started)
        assert store.save(prepared) == started
        for digest, body in prepared.blobs.items():
            assert store.blob(digest) == body
    with SQLiteWorkspaceTransactionStore(state) as reopened:
        assert reopened.load(prepared.plan.transaction_id) == started
        assert reopened.lookup("request-1") == started


def test_store_rejects_request_conflict_and_blob_tampering(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepared = _prepared(workspace)
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        store.save(prepared)
        conflicting = prepare_workspace_transaction(
            workspace,
            {"target.txt": DesiredWorkspaceFile(b"other\n", 0o644)},
            request_id="request-1",
            now=datetime(2026, 9, 8, 2, tzinfo=UTC),
        )
        with pytest.raises(KernelError) as conflict:
            store.save(conflicting)
        assert conflict.value.code == "delivery_request_conflict"
        digest = next(iter(prepared.blobs))
        (state / "blobs" / digest).write_bytes(b"tampered")
        with pytest.raises(KernelError) as corrupt:
            store.blob(digest)
        assert corrupt.value.code == "delivery_blob_corrupt"


def test_store_fails_closed_on_unknown_version_and_payload_corruption(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = sqlite3.connect(state / "transactions.db")
    database.execute(
        "CREATE TABLE delivery_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO delivery_metadata VALUES ('schema_version', '2')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteWorkspaceTransactionStore(state)
    assert version.value.code == "delivery_store_version"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepared = _prepared(workspace)
    valid_state = tmp_path / "valid-state"
    with SQLiteWorkspaceTransactionStore(valid_state) as store:
        store.save(prepared)
    database = sqlite3.connect(valid_state / "transactions.db")
    database.execute("UPDATE workspace_transactions SET payload='{}'")
    database.commit()
    database.close()
    with SQLiteWorkspaceTransactionStore(valid_state) as store:
        with pytest.raises(KernelError) as corrupt:
            store.load(prepared.plan.transaction_id)
    assert corrupt.value.code == "delivery_store_corrupt"
