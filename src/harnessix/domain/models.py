from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTION_SPEC_VERSION: Literal["harnessix.action/v1"] = "harnessix.action/v1"


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


class ExecutionOutcomeKind(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReconciliationOutcomeKind(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    MANUAL_INTERVENTION = "manual_intervention"


class Principal(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=128, description="租户标识")
    subject_id: str = Field(min_length=1, max_length=256, description="请求主体标识")
    framework: str = Field(min_length=1, max_length=64, description="上游 Agent 框架")
    roles: tuple[str, ...] = Field(default_factory=tuple, description="主体角色")


class ActionContext(ContractModel):
    session_id: str = Field(min_length=1, max_length=256, description="上游会话标识")
    run_id: str = Field(min_length=1, max_length=256, description="上游运行标识")
    trace_id: str | None = Field(default=None, max_length=256, description="链路追踪标识")


class TraceContext(ContractModel):
    """跨进程持久化的 W3C Trace Context。"""

    traceparent: str = Field(min_length=1, max_length=128, description="W3C traceparent")
    tracestate: str | None = Field(default=None, max_length=512, description="W3C tracestate")


class SecretRef(ContractModel):
    name: str = Field(min_length=1, max_length=256, description="凭据引用名称")
    version: str | None = Field(default=None, max_length=128, description="凭据版本")


class ActionRequest(ContractModel):
    spec_version: Literal["harnessix.action/v1"] = Field(
        default=ACTION_SPEC_VERSION, description="Action Contract 版本"
    )
    action_id: UUID = Field(default_factory=uuid4, description="Action 全局唯一标识")
    tool: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$",
        description="运行时注册的工具名称",
    )
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    principal: Principal
    context: ActionContext
    effect_hint: EffectClass | None = Field(
        default=None, description="调用方预期副作用；最终分类由运行时定义"
    )
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=256, description="租户范围业务幂等键"
    )
    secret_refs: tuple[SecretRef, ...] = Field(default_factory=tuple, description="凭据引用")
    metadata: dict[str, Any] = Field(default_factory=dict, description="非敏感扩展元数据")

    @model_validator(mode="after")
    def reject_blank_idempotency_key(self) -> Self:
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key 不能只包含空白字符")
        return self


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


class PolicyDecision(ContractModel):
    kind: PolicyDecisionKind
    policy_id: str
    reason: str
    evaluated_at: datetime = Field(default_factory=utc_now)


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


class ActionFailure(ContractModel):
    code: str
    message: str
    retriable: bool = False


class EffectReceipt(ContractModel):
    provider: str
    resource_type: str
    resource_id: str | None = None
    idempotency_key: str | None = None
    response_digest: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)


class ActionResult(ContractModel):
    status: ActionStatus
    output: Any | None = None
    error: ActionFailure | None = None
    receipt: EffectReceipt | None = None
    attempt: int = Field(default=1, ge=1)


class ActionSnapshot(ContractModel):
    request: ActionRequest
    request_fingerprint: str
    tool: ToolDescriptor
    status: ActionStatus
    trace_context: TraceContext | None = None
    policy: PolicyDecision | None = None
    approval: ApprovalRecord | None = None
    result: ActionResult | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class JournalOperationalStats(ContractModel):
    """Journal 对运维层暴露的低基数队列快照。"""

    ready_count: int = Field(ge=0)
    pending_approval_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    oldest_ready_at: datetime | None = None


class ActionEvent(ContractModel):
    action_id: UUID
    sequence: int = Field(ge=1)
    event_type: str
    from_status: ActionStatus | None = None
    to_status: ActionStatus
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ExecutionOutcome(ContractModel):
    kind: ExecutionOutcomeKind
    output: Any | None = None
    error: ActionFailure | None = None
    receipt: EffectReceipt | None = None

    @classmethod
    def succeeded(cls, *, output: Any, receipt: EffectReceipt | None = None) -> Self:
        return cls(kind=ExecutionOutcomeKind.SUCCEEDED, output=output, receipt=receipt)

    @classmethod
    def failed(cls, *, code: str, message: str, retriable: bool = False) -> Self:
        return cls(
            kind=ExecutionOutcomeKind.FAILED,
            error=ActionFailure(code=code, message=message, retriable=retriable),
        )

    @classmethod
    def unknown(cls, *, code: str, message: str) -> Self:
        return cls(
            kind=ExecutionOutcomeKind.UNKNOWN,
            error=ActionFailure(code=code, message=message, retriable=False),
        )


class ReconciliationOutcome(ContractModel):
    kind: ReconciliationOutcomeKind
    output: Any | None = None
    error: ActionFailure | None = None
    receipt: EffectReceipt | None = None

    @classmethod
    def succeeded(cls, *, output: Any, receipt: EffectReceipt) -> Self:
        return cls(kind=ReconciliationOutcomeKind.SUCCEEDED, output=output, receipt=receipt)

    @classmethod
    def failed(cls, *, code: str, message: str) -> Self:
        return cls(
            kind=ReconciliationOutcomeKind.FAILED,
            error=ActionFailure(code=code, message=message, retriable=False),
        )

    @classmethod
    def unknown(cls, *, code: str, message: str) -> Self:
        return cls(
            kind=ReconciliationOutcomeKind.UNKNOWN,
            error=ActionFailure(code=code, message=message, retriable=False),
        )

    @classmethod
    def manual(cls, *, code: str, message: str) -> Self:
        return cls(
            kind=ReconciliationOutcomeKind.MANUAL_INTERVENTION,
            error=ActionFailure(code=code, message=message, retriable=False),
        )
