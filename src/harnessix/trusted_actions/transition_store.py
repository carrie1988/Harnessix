"""可信Action状态迁移存储：以CAS追加连续Hash链审计。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, utc_now
from harnessix.trusted_actions.contracts import (
    ALLOWED_ROUTE_TRANSITIONS,
    ActionRouteSnapshot,
    ActionRouteState,
    ReconciliationConclusion,
    build_audit_event,
)
from harnessix.trusted_actions.recovery_contracts import ActionRuntimeFence


class ActionTransitionCommitMixin:
    """在调用方事务内提交Route状态与连续审计事件。"""

    _db: sqlite3.Connection

    if TYPE_CHECKING:

        def load(self, plan_id: UUID) -> ActionRouteSnapshot: ...

        def _assert_runtime_owner(self) -> ActionRuntimeFence | None: ...

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
    ) -> ActionRouteSnapshot:
        current = self.load(plan_id)
        if current.state not in expected:
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
        return self.load(plan_id)


class ActionTransitionStoreMixin(ActionTransitionCommitMixin):
    """集中Route状态CAS与审计事件同事务提交。"""

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
            self._assert_runtime_owner()
            self._transition_in_transaction(
                plan_id,
                expected=expected_set,
                target=target,
                approval_outcome=approval_outcome,
                approval_actor=approval_actor,
                executor_id=executor_id,
                output_sha256=output_sha256,
                artifact_sha256=artifact_sha256,
                external_action_id=external_action_id,
                error_code=error_code,
                reconciliation=reconciliation,
                occurred_at=occurred_at,
            )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(plan_id)
