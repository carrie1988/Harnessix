"""定义Coding Agent各运行时共享的基础领域契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EffectClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"
    DESTRUCTIVE = "destructive"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    POLICY_EVALUATED = "policy_evaluated"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    MANUAL_INTERVENTION = "manual_intervention"


TERMINAL_ACTION_STATUSES = frozenset(
    {
        ActionStatus.DENIED,
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.MANUAL_INTERVENTION,
    }
)

ALLOWED_ACTION_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.RECEIVED: frozenset({ActionStatus.VALIDATED, ActionStatus.FAILED}),
    ActionStatus.VALIDATED: frozenset({ActionStatus.POLICY_EVALUATED, ActionStatus.FAILED}),
    ActionStatus.POLICY_EVALUATED: frozenset(
        {ActionStatus.DENIED, ActionStatus.PENDING_APPROVAL, ActionStatus.READY}
    ),
    ActionStatus.PENDING_APPROVAL: frozenset({ActionStatus.DENIED, ActionStatus.READY}),
    ActionStatus.READY: frozenset({ActionStatus.LEASED}),
    ActionStatus.LEASED: frozenset({ActionStatus.READY, ActionStatus.RUNNING}),
    ActionStatus.RUNNING: frozenset(
        {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN}
    ),
    ActionStatus.UNKNOWN: frozenset({ActionStatus.RECONCILING, ActionStatus.MANUAL_INTERVENTION}),
    ActionStatus.RECONCILING: frozenset(
        {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.UNKNOWN,
            ActionStatus.MANUAL_INTERVENTION,
        }
    ),
    ActionStatus.DENIED: frozenset(),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.MANUAL_INTERVENTION: frozenset(),
}


class PolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class TraceContext(ContractModel):
    """跨进程持久化的 W3C Trace Context。"""

    traceparent: str = Field(min_length=1, max_length=128, description="W3C traceparent")
    tracestate: str | None = Field(default=None, max_length=512, description="W3C tracestate")


class ToolDescriptor(ContractModel):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    effect_class: EffectClass
    risk_level: RiskLevel
    requires_idempotency: bool
    requires_approval: bool
    supports_reconciliation: bool
    supports_parallel_calls: bool = False

    @model_validator(mode="after")
    def parallel_calls_are_read_only(self) -> Self:
        if self.supports_parallel_calls and self.effect_class is not EffectClass.READ_ONLY:
            raise ValueError("只有只读工具可以声明并行调用")
        return self


class ApprovalDecision(ContractModel):
    outcome: ApprovalOutcome
    actor: str = Field(min_length=1, max_length=256)
    reason: str | None = Field(default=None, max_length=2000)


class ApprovalRecord(ContractModel):
    outcome: ApprovalOutcome
    actor: str
    reason: str | None = None
    request_fingerprint: str
    decided_at: datetime = Field(default_factory=utc_now)
