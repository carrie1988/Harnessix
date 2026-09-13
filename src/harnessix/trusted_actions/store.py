"""以SQLite保存Action路由快照和连续Hash链审计，不持有Executor或Secret。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, utc_now
from harnessix.trusted_actions.contracts import (
    ALLOWED_ROUTE_TRANSITIONS,
    ActionAuditEvent,
    ActionRoutePlan,
    ActionRouteSnapshot,
    ActionRouteState,
    ReconciliationConclusion,
    build_audit_event,
)

_SCHEMA_VERSION = "1"


def _validate_plan(plan: ActionRoutePlan) -> ActionRoutePlan:
    try:
        return ActionRoutePlan.model_validate_json(plan.model_dump_json(warnings="error"))
    except (ValidationError, ValueError, TypeError):
        raise KernelError("action_route_plan_invalid", "Action Route Plan不符合契约") from None


class SQLiteActionAuditStore:
    """不可变Route Plan、当前投影和append-only审计事件。"""

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
            "CREATE TABLE IF NOT EXISTS action_audit_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM action_audit_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO action_audit_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("action_audit_store_version", "Action审计存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_route_plans (
                plan_id TEXT PRIMARY KEY,
                invocation_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS action_route_snapshots (
                plan_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                last_event_digest TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES action_route_plans(plan_id)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS action_audit_events (
                plan_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(plan_id, sequence),
                UNIQUE(digest),
                FOREIGN KEY(plan_id) REFERENCES action_route_plans(plan_id)
            ) STRICT;
            """
        )

    def save_plan(
        self, plan: ActionRoutePlan, *, initial_state: ActionRouteState
    ) -> ActionRouteSnapshot:
        if initial_state not in {"denied", "pending_approval", "ready"}:
            raise KernelError("action_route_state_invalid", "Action初始状态无效")
        checked = _validate_plan(plan)
        payload = checked.model_dump_json(warnings="error")
        now = utc_now()
        event = build_audit_event(
            checked,
            sequence=1,
            from_state=None,
            to_state=initial_state,
            previous_digest=None,
            error_code=(
                checked.execution.policy.reason_code if initial_state == "denied" else None
            ),
            occurred_at=now,
        )
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT invocation_id, fingerprint, payload FROM action_route_plans "
                "WHERE plan_id = ?",
                (str(checked.execution.plan_id),),
            ).fetchone()
            expected = (str(checked.invocation.invocation_id), checked.fingerprint, payload)
            if row is None:
                collision = self._db.execute(
                    "SELECT plan_id FROM action_route_plans WHERE invocation_id = ?",
                    (str(checked.invocation.invocation_id),),
                ).fetchone()
                if collision is not None:
                    raise KernelError(
                        "action_invocation_conflict", "Action调用标识已经绑定其他计划"
                    )
                self._db.execute(
                    "INSERT INTO action_route_plans VALUES (?, ?, ?, ?)",
                    (str(checked.execution.plan_id), *expected),
                )
                self._db.execute(
                    "INSERT INTO action_route_snapshots VALUES (?, ?, ?, ?, ?)",
                    (
                        str(checked.execution.plan_id),
                        initial_state,
                        1,
                        event.digest,
                        now.isoformat(),
                    ),
                )
                self._db.execute(
                    "INSERT INTO action_audit_events VALUES (?, ?, ?, ?)",
                    (
                        str(checked.execution.plan_id),
                        event.sequence,
                        event.digest,
                        event.model_dump_json(warnings="error"),
                    ),
                )
            elif row != expected:
                raise KernelError("action_route_plan_conflict", "Action Route Plan内容冲突")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(checked.execution.plan_id)

    def load(self, plan_id: UUID) -> ActionRouteSnapshot:
        plan_row = self._db.execute(
            "SELECT invocation_id, fingerprint, payload FROM action_route_plans WHERE plan_id = ?",
            (str(plan_id),),
        ).fetchone()
        snapshot_row = self._db.execute(
            "SELECT state, sequence, last_event_digest, updated_at "
            "FROM action_route_snapshots WHERE plan_id = ?",
            (str(plan_id),),
        ).fetchone()
        if plan_row is None or snapshot_row is None:
            raise KernelError("action_route_not_found", "Action Route Plan不存在")
        try:
            plan = ActionRoutePlan.model_validate_json(plan_row[2])
            event_row = self._db.execute(
                "SELECT digest, payload FROM action_audit_events "
                "WHERE plan_id = ? AND sequence = ?",
                (str(plan_id), snapshot_row[1]),
            ).fetchone()
            if event_row is None:
                raise ValueError
            event = ActionAuditEvent.model_validate_json(event_row[1])
            snapshot = ActionRouteSnapshot(
                plan=plan,
                state=snapshot_row[0],
                sequence=snapshot_row[1],
                last_event_digest=snapshot_row[2],
                updated_at=datetime.fromisoformat(snapshot_row[3]),
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("action_audit_store_corrupt", "Action审计记录损坏") from None
        try:
            indexed_invocation_id = UUID(plan_row[0])
        except (TypeError, ValueError):
            raise KernelError("action_audit_store_corrupt", "Action审计索引损坏") from None
        if (
            plan.execution.plan_id != plan_id
            or plan.invocation.invocation_id != indexed_invocation_id
            or plan.fingerprint != plan_row[1]
            or event.plan_id != plan_id
            or event.sequence != snapshot.sequence
            or event.to_state != snapshot.state
            or event.digest != snapshot.last_event_digest
            or event.digest != event_row[0]
        ):
            raise KernelError("action_audit_store_corrupt", "Action审计索引与事件不一致")
        return snapshot

    def events(self, plan_id: UUID) -> tuple[ActionAuditEvent, ...]:
        snapshot = self.load(plan_id)
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM action_audit_events "
            "WHERE plan_id = ? ORDER BY sequence",
            (str(plan_id),),
        ).fetchall()
        try:
            events = tuple(ActionAuditEvent.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("action_audit_store_corrupt", "Action审计事件损坏") from None
        previous: str | None = None
        state: str | None = None
        for index, (row, event) in enumerate(zip(rows, events, strict=True), start=1):
            if (
                row[0] != index
                or row[1] != event.digest
                or event.plan_id != plan_id
                or event.sequence != index
                or event.previous_digest != previous
                or event.from_state != state
            ):
                raise KernelError("action_audit_store_corrupt", "Action审计事件链损坏")
            previous = event.digest
            state = event.to_state
        if (
            len(events) != snapshot.sequence
            or previous != snapshot.last_event_digest
            or state != snapshot.state
        ):
            raise KernelError("action_audit_store_corrupt", "Action审计事件链不完整")
        return events

    def transition(
        self,
        plan_id: UUID,
        *,
        expected: Iterable[ActionRouteState],
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
    ) -> ActionRouteSnapshot:
        expected_set = frozenset(expected)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.load(plan_id)
            if current.state not in expected_set:
                raise KernelError("action_route_conflict", "Action状态与预期不一致")
            if target not in ALLOWED_ROUTE_TRANSITIONS[current.state]:
                raise KernelError("action_route_transition", "Action状态迁移不合法")
            now = occurred_at or utc_now()
            event = build_audit_event(
                current.plan,
                sequence=current.sequence + 1,
                from_state=current.state,
                to_state=target,
                previous_digest=current.last_event_digest,
                approval_outcome=approval_outcome,
                approval_actor=approval_actor,
                executor_id=executor_id,
                output_sha256=output_sha256,
                artifact_sha256=artifact_sha256,
                external_action_id=external_action_id,
                error_code=error_code,
                reconciliation=reconciliation,
                occurred_at=now,
            )
            updated = self._db.execute(
                "UPDATE action_route_snapshots SET state = ?, sequence = ?, "
                "last_event_digest = ?, updated_at = ? "
                "WHERE plan_id = ? AND state = ? AND sequence = ? AND last_event_digest = ?",
                (
                    target,
                    event.sequence,
                    event.digest,
                    now.isoformat(),
                    str(plan_id),
                    current.state,
                    current.sequence,
                    current.last_event_digest,
                ),
            )
            if updated.rowcount != 1:
                raise KernelError("action_route_conflict", "Action状态并发变化")
            self._db.execute(
                "INSERT INTO action_audit_events VALUES (?, ?, ?, ?)",
                (
                    str(plan_id),
                    event.sequence,
                    event.digest,
                    event.model_dump_json(warnings="error"),
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(plan_id)

    def active(self) -> tuple[ActionRouteSnapshot, ...]:
        rows = self._db.execute(
            "SELECT plan_id FROM action_route_snapshots "
            "WHERE state IN ('pending_approval', 'ready', 'running', 'unknown', 'reconciling') "
            "ORDER BY plan_id"
        ).fetchall()
        return tuple(self.load(UUID(row[0])) for row in rows)

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteActionAuditStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
