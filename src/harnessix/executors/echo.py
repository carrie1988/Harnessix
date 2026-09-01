from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from harnessix.domain.models import (
    ActionSnapshot,
    ExecutionOutcome,
    ReconciliationOutcome,
)


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(min_length=1, max_length=4000)


class EchoExecutor:
    async def execute(self, action: ActionSnapshot, arguments: BaseModel) -> ExecutionOutcome:
        parsed = EchoInput.model_validate(arguments)
        return ExecutionOutcome.succeeded(
            output={"message": parsed.message, "action_id": str(action.request.action_id)}
        )

    async def reconcile(self, action: ActionSnapshot) -> ReconciliationOutcome:
        return ReconciliationOutcome.manual(
            code="reconciliation_not_supported",
            message="system.echo 是只读工具，不需要对账",
        )
