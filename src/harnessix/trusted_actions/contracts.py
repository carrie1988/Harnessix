from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from harnessix.domain.models import (
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    utc_now,
)
from harnessix.execution.contracts import (
    ExecutionContract,
    ExecutionPlanV2,
    ToolSourceKind,
    canonical_digest,
)
from harnessix.tools.contracts import Revision

ActionResourceKind = Literal[
    "workspace",
    "process",
    "network",
    "secret",
    "git_ref",
    "external",
]
ActionResourceAccess = Literal["read", "write", "execute", "connect", "use", "update"]
RecoveryMode = Literal["none", "durable_ledger", "external_reconcile"]
ActionRouteState = Literal[
    "denied",
    "pending_approval",
    "ready",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "reconciling",
    "manual_intervention",
]
ReconciliationConclusion = Literal["succeeded", "failed", "unknown", "manual_intervention"]

TERMINAL_ROUTE_STATES = frozenset({"denied", "succeeded", "failed", "manual_intervention"})

ALLOWED_ROUTE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_approval": frozenset({"ready", "denied"}),
    "ready": frozenset({"running"}),
    "running": frozenset({"succeeded", "failed", "unknown"}),
    "unknown": frozenset({"reconciling", "manual_intervention"}),
    "reconciling": frozenset({"succeeded", "failed", "unknown", "manual_intervention"}),
    "denied": frozenset(),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "manual_intervention": frozenset(),
}


class CanonicalActionResource(ExecutionContract):
    spec_version: Literal["harnessix.action-resource/v1"] = "harnessix.action-resource/v1"
    kind: ActionResourceKind
    access: ActionResourceAccess
    identifier_sha256: Revision
    attributes_sha256: Revision

    @model_validator(mode="after")
    def compatible_kind_and_access(self) -> Self:
        allowed: dict[str, frozenset[str]] = {
            "workspace": frozenset({"read", "write", "execute"}),
            "process": frozenset({"execute"}),
            "network": frozenset({"connect"}),
            "secret": frozenset({"use"}),
            "git_ref": frozenset({"read", "update"}),
            "external": frozenset({"read", "write"}),
        }
        if self.access not in allowed[self.kind]:
            raise ValueError("Action资源类型与访问模式不兼容")
        return self


class TrustedToolBinding(ExecutionContract):
    spec_version: Literal["harnessix.trusted-tool-binding/v1"] = "harnessix.trusted-tool-binding/v1"
    source: ToolSourceKind
    source_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    tool_version: str = Field(min_length=1, max_length=128)
    tool_fingerprint: Revision
    input_schema_sha256: Revision
    effect_class: EffectClass
    risk_level: RiskLevel
    recovery_mode: RecoveryMode
    executor_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    binding_digest: Revision

    @model_validator(mode="after")
    def secure_recovery_contract(self) -> Self:
        if not self.source_id.strip() or not self.tool_version.strip():
            raise ValueError("Tool来源与版本不能为空白")
        if self.effect_class is EffectClass.READ_ONLY and self.recovery_mode != "none":
            raise ValueError("只读Tool不应声明副作用恢复")
        if self.effect_class is not EffectClass.READ_ONLY and self.recovery_mode == "none":
            raise ValueError("写入Tool必须声明可核对恢复边界")
        if (
            self.recovery_mode == "external_reconcile"
            and self.effect_class is EffectClass.READ_ONLY
        ):
            raise ValueError("外部对账只适用于写入Tool")
        if self.binding_digest != trusted_tool_binding_digest(self):
            raise ValueError("Trusted Tool绑定摘要不一致")
        return self


def trusted_tool_binding_digest(binding: TrustedToolBinding) -> str:
    return canonical_digest(
        binding.model_dump(mode="json", exclude={"binding_digest"}, warnings="error")
    )


def build_trusted_tool_binding(
    *,
    source: ToolSourceKind,
    source_id: str,
    tool: str,
    tool_version: str,
    tool_fingerprint: str,
    input_schema_sha256: str,
    effect_class: EffectClass,
    risk_level: RiskLevel,
    recovery_mode: RecoveryMode,
    executor_id: str,
) -> TrustedToolBinding:
    candidate = TrustedToolBinding.model_construct(
        _fields_set=None,
        source=source,
        source_id=source_id,
        tool=tool,
        tool_version=tool_version,
        tool_fingerprint=tool_fingerprint,
        input_schema_sha256=input_schema_sha256,
        effect_class=effect_class,
        risk_level=risk_level,
        recovery_mode=recovery_mode,
        executor_id=executor_id,
        binding_digest="0" * 64,
    )
    return TrustedToolBinding(
        **candidate.model_dump(exclude={"binding_digest"}),
        binding_digest=trusted_tool_binding_digest(candidate),
    )


class CodingActionInvocation(ExecutionContract):
    spec_version: Literal["harnessix.coding-action-invocation/v1"] = (
        "harnessix.coding-action-invocation/v1"
    )
    invocation_id: UUID = Field(default_factory=uuid4)
    source: ToolSourceKind
    source_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    tool_version: str = Field(min_length=1, max_length=128)
    tool_fingerprint: Revision
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def non_blank_identity(self) -> Self:
        if not self.source_id.strip() or not self.tool_version.strip():
            raise ValueError("Action调用来源与版本不能为空白")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("幂等键不能为空白")
        return self


class ActionRoutePlan(ExecutionContract):
    spec_version: Literal["harnessix.action-route-plan/v1"] = "harnessix.action-route-plan/v1"
    invocation: CodingActionInvocation
    binding: TrustedToolBinding
    resources: tuple[CanonicalActionResource, ...] = Field(max_length=512)
    resources_sha256: Revision
    execution: ExecutionPlanV2
    external_action_id: UUID | None = None
    fingerprint: Revision

    @model_validator(mode="after")
    def complete_route_binding(self) -> Self:
        identities = [
            (item.kind, item.access, item.identifier_sha256, item.attributes_sha256)
            for item in self.resources
        ]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("Action资源必须规范排序且唯一")
        intent = self.execution.intent
        invocation = self.invocation
        binding = self.binding
        if (
            self.execution.plan_id != invocation.invocation_id
            or (invocation.source, invocation.source_id, invocation.tool)
            != (binding.source, binding.source_id, binding.tool)
            or invocation.tool_version != binding.tool_version
            or invocation.tool_fingerprint != binding.tool_fingerprint
            or intent.source != binding.source
            or intent.source_id != binding.source_id
            or intent.tool != binding.tool
            or intent.tool_version != binding.tool_version
            or intent.tool_fingerprint != binding.tool_fingerprint
            or intent.arguments != invocation.arguments
            or intent.effect_class is not binding.effect_class
            or intent.risk_level is not binding.risk_level
            or intent.idempotency_key != invocation.idempotency_key
            or self.resources_sha256 != canonical_digest(identities)
            or (binding.recovery_mode == "external_reconcile")
            != (self.external_action_id is not None)
            or self.fingerprint != action_route_plan_fingerprint(self)
        ):
            raise ValueError("Action Route Plan绑定事实不一致")
        return self


def action_route_plan_fingerprint(plan: ActionRoutePlan) -> str:
    return canonical_digest(plan.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


class ActionExecutionOutcome(ExecutionContract):
    spec_version: Literal["harnessix.action-execution-outcome/v1"] = (
        "harnessix.action-execution-outcome/v1"
    )
    kind: ReconciliationConclusion
    output: JsonValue | None = None
    artifact_sha256: Revision | None = None
    external_action_id: UUID | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")

    @model_validator(mode="after")
    def terminal_shape(self) -> Self:
        if (self.kind == "succeeded") == (self.error_code is not None):
            raise ValueError("Action执行结果与错误字段不一致")
        return self


class ActionAuditEvent(ExecutionContract):
    spec_version: Literal["harnessix.action-audit-event/v1"] = "harnessix.action-audit-event/v1"
    plan_id: UUID
    plan_fingerprint: Revision
    sequence: int = Field(ge=1, le=256)
    from_state: ActionRouteState | None
    to_state: ActionRouteState
    resource_sha256: Revision
    policy_id: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=128)
    approval_outcome: ApprovalOutcome | None = None
    approval_actor_sha256: Revision | None = None
    executor_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    output_sha256: Revision | None = None
    artifact_sha256: Revision | None = None
    external_action_id: UUID | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    reconciliation: ReconciliationConclusion | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    previous_digest: Revision | None = None
    digest: Revision

    @model_validator(mode="after")
    def complete_audit_event(self) -> Self:
        if self.sequence == 1:
            chain_valid = self.from_state is None and self.previous_digest is None
        else:
            chain_valid = self.from_state is not None and self.previous_digest is not None
        approval_valid = (self.approval_outcome is None) == (self.approval_actor_sha256 is None)
        if not chain_valid or not approval_valid or self.digest != action_audit_event_digest(self):
            raise ValueError("Action审计事件链无效")
        return self


def action_audit_event_digest(event: ActionAuditEvent) -> str:
    return canonical_digest(event.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ActionRouteSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.action-route-snapshot/v1"] = (
        "harnessix.action-route-snapshot/v1"
    )
    plan: ActionRoutePlan
    state: ActionRouteState
    sequence: int = Field(ge=1, le=256)
    last_event_digest: Revision
    updated_at: AwareDatetime


def build_audit_event(
    plan: ActionRoutePlan,
    *,
    sequence: int,
    from_state: ActionRouteState | None,
    to_state: ActionRouteState,
    previous_digest: str | None,
    approval_outcome: ApprovalOutcome | None = None,
    approval_actor: str | None = None,
    executor_id: str | None = None,
    output_sha256: str | None = None,
    artifact_sha256: str | None = None,
    external_action_id: UUID | None = None,
    error_code: str | None = None,
    reconciliation: ReconciliationConclusion | None = None,
    occurred_at: datetime | None = None,
) -> ActionAuditEvent:
    actor_digest = canonical_digest(approval_actor) if approval_actor is not None else None
    candidate = ActionAuditEvent.model_construct(
        _fields_set=None,
        plan_id=plan.execution.plan_id,
        plan_fingerprint=plan.fingerprint,
        sequence=sequence,
        from_state=from_state,
        to_state=to_state,
        resource_sha256=plan.resources_sha256,
        policy_id=plan.execution.policy.policy_id,
        policy_version=plan.execution.policy.version,
        approval_outcome=approval_outcome,
        approval_actor_sha256=actor_digest,
        executor_id=executor_id,
        output_sha256=output_sha256,
        artifact_sha256=artifact_sha256,
        external_action_id=external_action_id,
        error_code=error_code,
        reconciliation=reconciliation,
        occurred_at=occurred_at or utc_now(),
        previous_digest=previous_digest,
        digest="0" * 64,
    )
    return ActionAuditEvent(
        **candidate.model_dump(exclude={"digest"}),
        digest=action_audit_event_digest(candidate),
    )
