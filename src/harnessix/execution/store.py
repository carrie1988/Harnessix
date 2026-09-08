from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import PolicyDecisionKind
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, ExecutionPlan

_SCHEMA_VERSION = "1"


class SQLiteExecutionPlanStore:
    """持久化不可变ExecutionPlan和一次性审批检查点。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._closed = False
        self._prepare_parent()
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

    def _prepare_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._path.parent.chmod(0o700)

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS execution_store_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM execution_store_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO execution_store_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("execution_store_version", "Execution Plan存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_plans (
                plan_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_revision TEXT NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE INDEX IF NOT EXISTS execution_plans_workspace
                ON execution_plans(workspace_id, workspace_revision);
            CREATE TABLE IF NOT EXISTS execution_approvals (
                plan_id TEXT PRIMARY KEY,
                plan_fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES execution_plans(plan_id)
            ) STRICT;
            """
        )

    def save_plan(self, plan: ExecutionPlan) -> None:
        checked = self._validate_plan(plan)
        payload = checked.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT fingerprint, payload FROM execution_plans WHERE plan_id = ?",
                (str(checked.plan_id),),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO execution_plans VALUES (?, ?, ?, ?, ?)",
                    (
                        str(checked.plan_id),
                        checked.fingerprint,
                        checked.workspace.workspace_id,
                        checked.workspace.revision,
                        payload,
                    ),
                )
            elif row != (checked.fingerprint, payload):
                raise KernelError("execution_plan_conflict", "Execution Plan标识已经绑定其他内容")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load_plan(self, plan_id: UUID) -> ExecutionPlan:
        row = self._db.execute(
            "SELECT payload FROM execution_plans WHERE plan_id = ?", (str(plan_id),)
        ).fetchone()
        if row is None:
            raise KernelError("execution_plan_not_found", "Execution Plan不存在")
        try:
            return ExecutionPlan.model_validate_json(row[0])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("execution_store_corrupt", "Execution Plan存储记录损坏") from None

    def record_approval(self, checkpoint: ExecutionApprovalCheckpoint) -> None:
        checked = self._validate_approval(checkpoint)
        payload = checked.model_dump_json(warnings="error")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT payload FROM execution_plans WHERE plan_id = ?",
                (str(checked.plan_id),),
            ).fetchone()
            if row is None:
                raise KernelError("execution_plan_not_found", "审批对应的Execution Plan不存在")
            try:
                plan = ExecutionPlan.model_validate_json(row[0])
            except (ValidationError, ValueError, TypeError):
                raise KernelError("execution_store_corrupt", "Execution Plan存储记录损坏") from None
            if (
                plan.fingerprint != checked.plan_fingerprint
                or plan.policy.decision is not PolicyDecisionKind.REQUIRE_APPROVAL
            ):
                raise KernelError("approval_plan_mismatch", "审批没有绑定待批准Execution Plan")
            existing = self._db.execute(
                "SELECT plan_fingerprint, payload FROM execution_approvals WHERE plan_id = ?",
                (str(checked.plan_id),),
            ).fetchone()
            if existing is None:
                self._db.execute(
                    "INSERT INTO execution_approvals VALUES (?, ?, ?)",
                    (str(checked.plan_id), checked.plan_fingerprint, payload),
                )
            elif existing != (checked.plan_fingerprint, payload):
                raise KernelError("approval_conflict", "Execution Plan已经记录其他审批决定")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load_approval(self, plan_id: UUID) -> ExecutionApprovalCheckpoint | None:
        row = self._db.execute(
            "SELECT payload FROM execution_approvals WHERE plan_id = ?", (str(plan_id),)
        ).fetchone()
        if row is None:
            return None
        try:
            return ExecutionApprovalCheckpoint.model_validate_json(row[0])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("execution_store_corrupt", "Execution审批存储记录损坏") from None

    @staticmethod
    def _validate_plan(plan: ExecutionPlan) -> ExecutionPlan:
        try:
            return ExecutionPlan.model_validate_json(plan.model_dump_json(warnings="error"))
        except (ValidationError, ValueError, TypeError):
            raise KernelError("execution_plan_invalid", "Execution Plan不符合持久化契约") from None

    @staticmethod
    def _validate_approval(
        checkpoint: ExecutionApprovalCheckpoint,
    ) -> ExecutionApprovalCheckpoint:
        try:
            return ExecutionApprovalCheckpoint.model_validate_json(
                checkpoint.model_dump_json(warnings="error")
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("approval_invalid", "Execution审批不符合持久化契约") from None

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteExecutionPlanStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
