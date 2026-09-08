from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.processes.supervision_contracts import (
    ProcessLease,
    ProcessLeaseState,
    process_lease_binding,
)

_SCHEMA_VERSION = "1"
_TRANSITIONS: dict[ProcessLeaseState, frozenset[ProcessLeaseState]] = {
    "prepared": frozenset({"starting", "failed", "unknown"}),
    "starting": frozenset({"running", "exited", "failed", "unknown"}),
    "running": frozenset({"running", "stopping", "exited", "unknown"}),
    "stopping": frozenset({"stopping", "exited", "unknown"}),
    "exited": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
}


class SQLiteProcessLeaseStore:
    """以完整快照事件持久化Process Lease；不以数字PID作为恢复权限。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._closed = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._path.parent.chmod(0o700)
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if os.name == "posix":
                self._path.chmod(0o600)
            self._initialize()
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS process_store_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM process_store_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO process_store_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("process_store_version", "Process Lease存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS process_leases (
                process_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                plan_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE INDEX IF NOT EXISTS process_leases_state ON process_leases(state);
            CREATE TABLE IF NOT EXISTS process_lease_events (
                process_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(process_id, sequence),
                FOREIGN KEY(process_id) REFERENCES process_leases(process_id)
            ) STRICT;
            """
        )

    def create(self, lease: ProcessLease) -> None:
        checked = self._validate(lease)
        if checked.state != "prepared" or checked.sequence != 0:
            raise KernelError("process_lease_invalid", "新Process Lease必须从prepared/0开始")
        payload = checked.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT plan_id, plan_fingerprint, state, sequence, payload "
                "FROM process_leases WHERE process_id = ?",
                (str(checked.process_id),),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO process_leases VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(checked.process_id),
                        str(checked.plan_id),
                        checked.plan_fingerprint,
                        checked.state,
                        checked.sequence,
                        payload,
                    ),
                )
                self._db.execute(
                    "INSERT INTO process_lease_events VALUES (?, ?, ?, ?)",
                    (str(checked.process_id), checked.sequence, checked.state, payload),
                )
            elif row != (
                str(checked.plan_id),
                checked.plan_fingerprint,
                checked.state,
                checked.sequence,
                payload,
            ):
                raise KernelError("process_lease_conflict", "Process ID已经绑定其他Lease")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def transition(self, current: ProcessLease, updated: ProcessLease) -> None:
        before = self._validate(current)
        after = self._validate(updated)
        if (
            process_lease_binding(before) != process_lease_binding(after)
            or after.sequence != before.sequence + 1
            or after.state not in _TRANSITIONS[before.state]
        ):
            raise KernelError("process_transition_invalid", "Process Lease状态迁移无效")
        payload = after.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT state, sequence, payload FROM process_leases WHERE process_id = ?",
                (str(before.process_id),),
            ).fetchone()
            if row is None:
                raise KernelError("process_lease_not_found", "Process Lease不存在")
            if row != (
                before.state,
                before.sequence,
                before.model_dump_json(warnings="error"),
            ):
                raise KernelError("process_lease_stale", "Process Lease已被其他owner推进")
            self._db.execute(
                "UPDATE process_leases SET state = ?, sequence = ?, payload = ? "
                "WHERE process_id = ? AND sequence = ?",
                (
                    after.state,
                    after.sequence,
                    payload,
                    str(after.process_id),
                    before.sequence,
                ),
            )
            self._db.execute(
                "INSERT INTO process_lease_events VALUES (?, ?, ?, ?)",
                (str(after.process_id), after.sequence, after.state, payload),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise KernelError("process_lease_stale", "Process Lease事件序号冲突") from None
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load(self, process_id: UUID) -> ProcessLease:
        row = self._db.execute(
            "SELECT process_id, plan_id, plan_fingerprint, state, sequence, payload "
            "FROM process_leases WHERE process_id = ?",
            (str(process_id),),
        ).fetchone()
        if row is None:
            raise KernelError("process_lease_not_found", "Process Lease不存在")
        return self._decode_current(row)

    def active(self) -> tuple[ProcessLease, ...]:
        rows = self._db.execute(
            "SELECT process_id, plan_id, plan_fingerprint, state, sequence, payload "
            "FROM process_leases ORDER BY process_id"
        ).fetchall()
        leases = tuple(self._decode_current(row) for row in rows)
        return tuple(
            lease for lease in leases if lease.state not in {"exited", "failed", "unknown"}
        )

    def _decode_current(self, row: tuple[object, ...]) -> ProcessLease:
        if len(row) != 6 or not isinstance(row[5], str):
            raise KernelError("process_store_corrupt", "Process Lease当前记录损坏")
        lease = self._decode(row[5])
        expected = (
            str(lease.process_id),
            str(lease.plan_id),
            lease.plan_fingerprint,
            lease.state,
            lease.sequence,
        )
        if row[:5] != expected:
            raise KernelError("process_store_corrupt", "Process Lease索引与正文不一致")
        event = self._db.execute(
            "SELECT state, payload FROM process_lease_events WHERE process_id = ? AND sequence = ?",
            (str(lease.process_id), lease.sequence),
        ).fetchone()
        if event != (lease.state, row[5]):
            raise KernelError("process_store_corrupt", "Process Lease最新事件与当前记录不一致")
        return lease

    @staticmethod
    def _decode(payload: str) -> ProcessLease:
        try:
            return ProcessLease.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("process_store_corrupt", "Process Lease存储记录损坏") from None

    @staticmethod
    def _validate(lease: ProcessLease) -> ProcessLease:
        try:
            return ProcessLease.model_validate_json(lease.model_dump_json(warnings="error"))
        except (ValidationError, ValueError, TypeError):
            raise KernelError("process_lease_invalid", "Process Lease不符合持久化契约") from None

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteProcessLeaseStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
