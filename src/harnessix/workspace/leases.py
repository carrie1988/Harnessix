from __future__ import annotations

import math
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import WorkspaceLease


class WorkspaceLeaseStore:
    """SQLite跨进程租约；副作用端口必须在每次提交前检查fencing token。"""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._closed = False
        self._prepare_parent()
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        if os.name == "posix":
            self._path.chmod(0o600)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_leases (
                workspace_id TEXT PRIMARY KEY,
                owner_id TEXT,
                fencing_token INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                updated_at REAL NOT NULL
            ) STRICT
            """
        )

    def _prepare_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._path.parent.chmod(0o700)

    def acquire(
        self, workspace_id: Revision, owner_id: str, *, ttl_seconds: float
    ) -> WorkspaceLease:
        self._validate(workspace_id, owner_id, ttl_seconds)
        now = self._clock()
        expires = now + ttl_seconds
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT owner_id, fencing_token, expires_at FROM workspace_leases "
                "WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                token = 1
                self._db.execute(
                    "INSERT INTO workspace_leases VALUES (?, ?, ?, ?, ?)",
                    (workspace_id, owner_id, token, expires, now),
                )
            else:
                current_owner, current_token, current_expiry = row
                if current_owner == owner_id and current_expiry > now:
                    token = int(current_token)
                elif current_owner is not None and current_expiry > now:
                    raise KernelError("workspace_busy", "Workspace由其他宿主持有")
                else:
                    token = int(current_token) + 1
                self._db.execute(
                    "UPDATE workspace_leases SET owner_id = ?, fencing_token = ?, "
                    "expires_at = ?, updated_at = ? WHERE workspace_id = ?",
                    (owner_id, token, expires, now, workspace_id),
                )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return WorkspaceLease(
            workspace_id=workspace_id,
            owner_id=owner_id,
            fencing_token=token,
            expires_at=expires,
        )

    def renew(self, lease: WorkspaceLease, *, ttl_seconds: float) -> WorkspaceLease:
        self._validate(lease.workspace_id, lease.owner_id, ttl_seconds)
        now = self._clock()
        expires = now + ttl_seconds
        cursor = self._db.execute(
            "UPDATE workspace_leases SET expires_at = ?, updated_at = ? "
            "WHERE workspace_id = ? AND owner_id = ? AND fencing_token = ? "
            "AND expires_at > ?",
            (
                expires,
                now,
                lease.workspace_id,
                lease.owner_id,
                lease.fencing_token,
                now,
            ),
        )
        if cursor.rowcount != 1:
            raise KernelError("workspace_lease_lost", "Workspace租约已失效")
        return lease.model_copy(update={"expires_at": expires})

    def assert_current(self, lease: WorkspaceLease) -> None:
        now = self._clock()
        row = self._db.execute(
            "SELECT owner_id, fencing_token, expires_at FROM workspace_leases "
            "WHERE workspace_id = ?",
            (lease.workspace_id,),
        ).fetchone()
        if (
            row != (lease.owner_id, lease.fencing_token, lease.expires_at)
            or lease.expires_at <= now
        ):
            raise KernelError("workspace_lease_lost", "Workspace fencing token不再有效")

    def release(self, lease: WorkspaceLease) -> None:
        now = self._clock()
        cursor = self._db.execute(
            "UPDATE workspace_leases SET owner_id = NULL, expires_at = 0, updated_at = ? "
            "WHERE workspace_id = ? AND owner_id = ? AND fencing_token = ?",
            (now, lease.workspace_id, lease.owner_id, lease.fencing_token),
        )
        if cursor.rowcount != 1:
            raise KernelError("workspace_lease_lost", "Workspace租约已被替换")

    @staticmethod
    def _validate(workspace_id: str, owner_id: str, ttl_seconds: float) -> None:
        if (
            type(workspace_id) is not str
            or type(owner_id) is not str
            or type(ttl_seconds) not in {int, float}
            or len(workspace_id) != 64
            or any(character not in "0123456789abcdef" for character in workspace_id)
            or not owner_id
            or len(owner_id) > 128
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 3600
        ):
            raise KernelError("workspace_lease_invalid", "Workspace租约参数无效")

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> WorkspaceLeaseStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
