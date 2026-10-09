"""以SQLite不可变保存Execution Plan与Approval Checkpoint，并校验重复写一致性。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import PolicyDecisionKind
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionPlan,
    ExecutionPlanV2,
)
from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.sqlite_readonly import readonly_database
from harnessix.workspace.blob_read_control import workspace_blob_read_boundary
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure

_SCHEMA_VERSION = "1"
_PARENT_CLOSURE_SCHEMA_VERSION = "2"
ExecutionPlanAny = ExecutionPlan | ExecutionPlanV2 | ExecutionPlanV3
_LEGACY_PLAN_ADAPTER: TypeAdapter[ExecutionPlan | ExecutionPlanV2] = TypeAdapter(
    ExecutionPlan | ExecutionPlanV2
)
_PLAN_ADAPTER: TypeAdapter[ExecutionPlanAny] = TypeAdapter(
    Annotated[ExecutionPlanAny, Field(discriminator="spec_version")]
)


class _ExecutionStoreSchema:
    """仅初始化和读取执行存储代际，不在只读打开时迁移。"""

    _db: sqlite3.Connection

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
        elif row[0] not in {_SCHEMA_VERSION, _PARENT_CLOSURE_SCHEMA_VERSION}:
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

    def _check_schema(self) -> None:
        row = self._db.execute(
            "SELECT value FROM execution_store_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row[0] not in {_SCHEMA_VERSION, _PARENT_CLOSURE_SCHEMA_VERSION}:
            raise KernelError("execution_store_version", "Execution Plan存储版本不受支持")


class _ExecutionPlanValidation:
    """严格分派领域代际，并在完整父历史验真后返回计划。"""

    _read_blob: Callable[[str], bytes] | None
    _checkpoint: Callable[[], None]

    def _validate_plan(self, plan: ExecutionPlanAny) -> ExecutionPlanAny:
        try:
            payload = plan.model_dump_json(warnings="error")
        except (ValidationError, ValueError, TypeError):
            raise KernelError("execution_plan_invalid", "Execution Plan不符合持久化契约") from None
        return self._decode_plan(payload, error_code="execution_plan_invalid")

    def _decode_plan(self, payload: str, *, error_code: str) -> ExecutionPlanAny:
        try:
            value = json.loads(payload)
            if isinstance(value, dict) and "spec_version" not in value:
                # 缺省标签仅沿原旧代际Reader解析，不补签或降级新历史。
                plan: ExecutionPlanAny = _LEGACY_PLAN_ADAPTER.validate_json(payload, strict=True)
            else:
                plan = _PLAN_ADAPTER.validate_json(payload, strict=True)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise KernelError(error_code, "Execution Plan不符合持久化契约") from None
        if isinstance(plan, ExecutionPlanV3):
            if self._read_blob is None:
                raise KernelError(error_code, "Execution Plan缺少完整父目录历史读取端口")
            with workspace_blob_read_boundary(self._read_blob) as read:
                try:
                    read_workspace_parent_closure(plan.workspace, read, checkpoint=self._checkpoint)
                except KernelError as error:
                    if error.code != "workspace_closure_corrupt":
                        raise
                    raise KernelError(error_code, "Execution Plan父目录历史损坏") from None
        return plan

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


class SQLiteExecutionPlanStore(_ExecutionStoreSchema, _ExecutionPlanValidation):
    """持久化不可变ExecutionPlan和一次性审批检查点。"""

    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        read_blob: Callable[[str], bytes] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._closed = False
        self._read_blob = read_blob
        self._checkpoint = checkpoint if checkpoint is not None else lambda: None
        if read_only:
            self._db = readonly_database(self._path)
            try:
                self._check_schema()
            except BaseException:
                self.close()
                raise
            return
        _prepare_parent(self._path)
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

    def save_plan(self, plan: ExecutionPlanAny) -> None:
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
            if isinstance(checked, ExecutionPlanV3):
                self._db.execute(
                    "UPDATE execution_store_metadata SET value = ? "
                    "WHERE key = 'schema_version' AND value = '1'",
                    (_PARENT_CLOSURE_SCHEMA_VERSION,),
                )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load_plan(self, plan_id: UUID) -> ExecutionPlanAny:
        row = self._db.execute(
            "SELECT payload FROM execution_plans WHERE plan_id = ?", (str(plan_id),)
        ).fetchone()
        if row is None:
            raise KernelError("execution_plan_not_found", "Execution Plan不存在")
        return self._decode_plan(row[0], error_code="execution_store_corrupt")

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
            plan = self._decode_plan(row[0], error_code="execution_store_corrupt")
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
        plan = self.load_plan(plan_id)
        try:
            checkpoint = ExecutionApprovalCheckpoint.model_validate_json(row[0], strict=True)
            if (
                checkpoint.plan_id != plan_id
                or checkpoint.plan_fingerprint != plan.fingerprint
                or plan.policy.decision is not PolicyDecisionKind.REQUIRE_APPROVAL
            ):
                raise ValueError
            return checkpoint
        except (ValidationError, ValueError, TypeError):
            raise KernelError("execution_store_corrupt", "Execution审批存储记录损坏") from None

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteExecutionPlanStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _prepare_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
