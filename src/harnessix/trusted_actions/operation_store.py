"""可信Action操作存储：原子持久化Route期限、尝试与完成状态。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, utc_now
from harnessix.trusted_actions.contracts import (
    ActionRouteSnapshot,
    ActionRouteState,
    ReconciliationConclusion,
)
from harnessix.trusted_actions.recovery_contracts import (
    ActionOperationPhase,
    ActionRouteOperation,
    ActionRuntimeFence,
    ClaimedActionOperation,
)


def initialize_action_operation_schema(database: sqlite3.Connection) -> None:
    """为Schema v1前向增加有期限的Route操作账本。"""

    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS action_route_operations (
            operation_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('execute', 'reconcile')),
            attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 128),
            owner_generation INTEGER NOT NULL CHECK (owner_generation >= 1),
            owner_token_sha256 TEXT NOT NULL CHECK (length(owner_token_sha256) = 64),
            started_at TEXT NOT NULL,
            deadline TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('active', 'completed', 'interrupted')),
            completion_code TEXT,
            completed_at TEXT,
            UNIQUE(plan_id, phase, attempt),
            FOREIGN KEY(plan_id) REFERENCES action_route_plans(plan_id)
        ) STRICT;
        CREATE INDEX IF NOT EXISTS action_route_operations_active
            ON action_route_operations(state, deadline, plan_id);
        """
    )


class ActionOperationStoreSupport:
    """声明Operation切片依赖的Action Audit内部能力。"""

    _path: Path
    _db: sqlite3.Connection
    _require_runtime_owner: bool
    _runtime_fence: ActionRuntimeFence | None

    if TYPE_CHECKING:

        def load(self, plan_id: UUID) -> ActionRouteSnapshot: ...

        def _assert_runtime_owner(self) -> ActionRuntimeFence | None: ...

        @staticmethod
        def _token_digest(token: str) -> str: ...

        def _transition_in_transaction(
            self,
            plan_id: UUID,
            *,
            expected: frozenset[ActionRouteState],
            target: ActionRouteState,
            approval_outcome: ApprovalOutcome | None = None,
            approval_actor: str | None = None,
            executor_id: str | None = None,
            output_sha256: str | None = None,
            artifact_sha256: str | None = None,
            external_action_id: UUID | None = None,
            error_code: str | None = None,
            reconciliation: ReconciliationConclusion | None = None,
            occurred_at: datetime | None = None,
        ) -> ActionRouteSnapshot: ...


class ActionOperationClaimMixin(ActionOperationStoreSupport):
    """原子取得Execute或Reconcile操作租约。"""

    def claim_operation(
        self,
        plan_id: UUID,
        *,
        phase: ActionOperationPhase,
        timeout_seconds: float,
        max_attempts: int = 128,
    ) -> ClaimedActionOperation:
        """将Route迁入执行态并原子持久化期限、Owner代次和不可公开的能力摘要。"""

        if not 0 < timeout_seconds <= 86400 or not 1 <= max_attempts <= 128:
            raise KernelError("action_route_limits_invalid", "Action Route期限或尝试上限无效")
        token = os.urandom(32).hex()
        now = utc_now()
        deadline = now + timedelta(seconds=timeout_seconds)
        expected: frozenset[ActionRouteState] = frozenset(
            {"ready"} if phase == "execute" else {"unknown"}
        )
        target: ActionRouteState = "running" if phase == "execute" else "reconciling"
        try:
            self._db.execute("BEGIN IMMEDIATE")
            fence = self._assert_runtime_owner()
            if fence is None:
                # 非产品测试Store仍获得可验证的进程内代次，不放宽操作租约合同。
                fence = ActionRuntimeFence(generation=1, token="0" * 64, acquired_at=now)
            row = self._db.execute(
                "SELECT COALESCE(MAX(attempt), 0) FROM action_route_operations "
                "WHERE plan_id = ? AND phase = ?",
                (str(plan_id), phase),
            ).fetchone()
            attempt = int(row[0]) + 1
            if attempt > max_attempts:
                raise KernelError("action_reconciliation_exhausted", "Action对账尝试已经耗尽")
            current = self.load(plan_id)
            operation = ActionRouteOperation(
                operation_id=uuid4(),
                plan_id=plan_id,
                phase=phase,
                attempt=attempt,
                owner_generation=fence.generation,
                owner_token_sha256=self._token_digest(token),
                started_at=now,
                deadline=deadline,
            )
            self._transition_in_transaction(
                plan_id,
                expected=expected,
                target=target,
                executor_id=current.plan.binding.executor_id,
                external_action_id=current.plan.external_action_id,
            )
            self._db.execute(
                "INSERT INTO action_route_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(operation.operation_id),
                    str(plan_id),
                    phase,
                    attempt,
                    operation.owner_generation,
                    operation.owner_token_sha256,
                    now.isoformat(),
                    deadline.isoformat(),
                    "active",
                    None,
                    None,
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return ClaimedActionOperation(operation=operation, token=token)


class ActionOperationCompleteMixin(ActionOperationStoreSupport):
    """校验Fence与能力Token后原子提交操作结果。"""

    def complete_operation(
        self,
        claim: ClaimedActionOperation,
        *,
        target: ActionRouteState,
        executor_id: str,
        output_sha256: str | None = None,
        artifact_sha256: str | None = None,
        external_action_id: UUID | None = None,
        error_code: str | None = None,
        reconciliation: ReconciliationConclusion | None = None,
    ) -> ActionRouteSnapshot:
        """仅当前Fence和Operation能力可提交终态；操作记录与审计迁移同事务。"""

        operation = claim.operation
        expected: frozenset[ActionRouteState] = frozenset(
            {"running"} if operation.phase == "execute" else {"reconciling"}
        )
        completion_code = error_code or target
        now = utc_now()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            fence = self._assert_runtime_owner()
            if fence is not None and (operation.owner_generation != fence.generation):
                raise KernelError("action_runtime_fence_lost", "Action操作Owner已经失效")
            row = self._db.execute(
                "SELECT owner_generation, owner_token_sha256, state FROM action_route_operations "
                "WHERE operation_id = ? AND plan_id = ? AND phase = ? AND attempt = ?",
                (
                    str(operation.operation_id),
                    str(operation.plan_id),
                    operation.phase,
                    operation.attempt,
                ),
            ).fetchone()
            if row != (
                operation.owner_generation,
                operation.owner_token_sha256,
                "active",
            ) or operation.owner_token_sha256 != self._token_digest(claim.token):
                raise KernelError("action_operation_stale", "Action操作能力已经失效")
            self._transition_in_transaction(
                operation.plan_id,
                expected=expected,
                target=target,
                executor_id=executor_id,
                output_sha256=output_sha256,
                artifact_sha256=artifact_sha256,
                external_action_id=external_action_id,
                error_code=error_code,
                reconciliation=reconciliation,
            )
            updated = self._db.execute(
                "UPDATE action_route_operations SET state = 'completed', completion_code = ?, "
                "completed_at = ? WHERE operation_id = ? AND state = 'active'",
                (completion_code, now.isoformat(), str(operation.operation_id)),
            )
            if updated.rowcount != 1:
                raise KernelError("action_operation_stale", "Action操作状态并发变化")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(operation.plan_id)


class ActionOperationRecoveryMixin(ActionOperationStoreSupport):
    """中断遗留Operation并提供恢复扫描所需的只读清单。"""

    def interrupt_operation(self, plan_id: UUID, *, error_code: str) -> ActionRouteSnapshot:
        """新Owner将遗留执行原子标为UNKNOWN；不会调用Execute或伪造确定终态。"""

        now = utc_now()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._assert_runtime_owner()
            current = self.load(plan_id)
            if current.state not in {"running", "reconciling"}:
                self._db.execute("COMMIT")
                return current
            phase = "execute" if current.state == "running" else "reconcile"
            self._db.execute(
                "UPDATE action_route_operations SET state = 'interrupted', "
                "completion_code = ?, completed_at = ? "
                "WHERE operation_id = (SELECT operation_id FROM action_route_operations "
                "WHERE plan_id = ? AND phase = ? AND state = 'active' "
                "ORDER BY attempt DESC LIMIT 1) AND state = 'active'",
                (error_code, now.isoformat(), str(plan_id), phase),
            )
            self._transition_in_transaction(
                plan_id,
                expected=frozenset({current.state}),
                target="unknown",
                executor_id=current.plan.binding.executor_id,
                external_action_id=current.plan.external_action_id,
                error_code=error_code,
                reconciliation=("unknown" if current.state == "reconciling" else None),
                occurred_at=now,
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(plan_id)

    def operations(self, *, active_only: bool = False) -> tuple[ActionRouteOperation, ...]:
        where = "WHERE state = 'active'" if active_only else ""
        rows = self._db.execute(
            "SELECT operation_id, plan_id, phase, attempt, owner_generation, "
            "owner_token_sha256, started_at, deadline, state, completion_code, completed_at "
            f"FROM action_route_operations {where} ORDER BY plan_id, phase, attempt"
        ).fetchall()
        try:
            return tuple(
                ActionRouteOperation(
                    operation_id=UUID(row[0]),
                    plan_id=UUID(row[1]),
                    phase=row[2],
                    attempt=row[3],
                    owner_generation=row[4],
                    owner_token_sha256=row[5],
                    started_at=datetime.fromisoformat(row[6]),
                    deadline=datetime.fromisoformat(row[7]),
                    state=row[8],
                    completion_code=row[9],
                    completed_at=(datetime.fromisoformat(row[10]) if row[10] else None),
                )
                for row in rows
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("action_audit_store_corrupt", "Action操作记录损坏") from None

    def routes(self) -> tuple[ActionRouteSnapshot, ...]:
        """按稳定Plan ID返回全部Route；用于有界启动完整性扫描。"""

        rows = self._db.execute(
            "SELECT plan_id FROM action_route_snapshots ORDER BY plan_id"
        ).fetchall()
        try:
            return tuple(self.load(UUID(row[0])) for row in rows)
        except (TypeError, ValueError):
            raise KernelError("action_audit_store_corrupt", "Action Route索引损坏") from None


class ActionOperationStoreMixin(
    ActionOperationClaimMixin,
    ActionOperationCompleteMixin,
    ActionOperationRecoveryMixin,
):
    """组合Action操作租约、完成与中断恢复能力。"""
