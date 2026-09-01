from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from harnessix.domain.models import (
    ActionSnapshot,
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
