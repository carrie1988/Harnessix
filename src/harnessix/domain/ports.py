from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from harnessix.domain.models import (
    ActionEvent,
    ActionRequest,
    ActionResult,
    ActionSnapshot,
    ActionStatus,
    ApprovalRecord,
    ExecutionOutcome,
    PolicyDecision,
    ReconciliationOutcome,
    ToolDescriptor,
)


class ActionExecutor(Protocol):
    async def execute(self, action: ActionSnapshot, arguments: BaseModel) -> ExecutionOutcome: ...

    async def reconcile(self, action: ActionSnapshot) -> ReconciliationOutcome: ...


class PolicyEngine(Protocol):
    async def evaluate(self, action: ActionSnapshot, tool: ToolDescriptor) -> PolicyDecision: ...


class EffectJournal(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def create_action(
        self,
        request: ActionRequest,
        tool: ToolDescriptor,
        request_fingerprint: str,
    ) -> tuple[ActionSnapshot, bool]: ...

    async def get_action(self, action_id: UUID | str) -> ActionSnapshot: ...

    async def list_events(self, action_id: UUID | str) -> list[ActionEvent]: ...

    async def transition(
        self,
        action_id: UUID | str,
        *,
        expected: Iterable[ActionStatus],
        target: ActionStatus,
        event_type: str,
        data: dict[str, Any] | None = None,
        policy: PolicyDecision | None = None,
        approval: ApprovalRecord | None = None,
        result: ActionResult | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        clear_lease: bool = False,
        required_lease_owner: str | None = None,
    ) -> ActionSnapshot: ...

    async def claim_next_ready(
        self, *, worker_id: str, lease_expires_at: datetime
    ) -> ActionSnapshot | None: ...

    async def renew_lease(
        self, action_id: UUID | str, *, worker_id: str, lease_expires_at: datetime
    ) -> bool: ...

    async def recover_expired(self, now: datetime | None = None) -> list[UUID]: ...
