"""可信执行计划：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from harnessix.domain.models import (
    ApprovalOutcome,
    ApprovalRecord,
    ContractModel,
    EffectClass,
    PolicyDecisionKind,
    RiskLevel,
)
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import PlatformKind, WorkspaceSnapshot

SandboxLevel = Literal["host_guarded", "host_sandboxed", "container_strong"]
NetworkMode = Literal["none", "limited", "restricted", "full"]
ToolSourceKind = Literal["builtin", "mcp", "skill", "hook", "custom"]


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class ExecutionContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class EnvironmentBinding(ExecutionContract):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    value_sha256: Revision


class SecretVersionBinding(ExecutionContract):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    version: str = Field(min_length=1, max_length=128)
    target: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class SandboxBinding(ExecutionContract):
    level: SandboxLevel
    backend: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    backend_version: str = Field(min_length=1, max_length=128)
    network: NetworkMode
    capability_digest: Revision

    @model_validator(mode="after")
    def level_backend_consistent(self) -> Self:
        if self.level == "host_guarded" and self.backend != "host":
            raise ValueError("host_guarded必须使用host后端")
        if self.level == "container_strong" and self.backend not in {"docker", "podman"}:
            raise ValueError("container_strong必须使用受支持容器后端")
        return self


class ExecutionPolicyBinding(ExecutionContract):
    version: str = Field(min_length=1, max_length=128)
    decision: PolicyDecisionKind
    policy_id: str = Field(min_length=1, max_length=256)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")


class ExecutionIntent(ExecutionContract):
    spec_version: Literal["harnessix.execution-intent/v1"] = "harnessix.execution-intent/v1"
    source: ToolSourceKind
    source_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    tool_version: str = Field(min_length=1, max_length=128)
    tool_fingerprint: Revision
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    effect_class: EffectClass
    risk_level: RiskLevel
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def effect_requirements(self) -> Self:
        if not self.source_id.strip() or not self.tool_version.strip():
            raise ValueError("执行意图来源和工具版本不能为空白")
        if self.effect_class in {EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.DESTRUCTIVE}:
            if self.idempotency_key is None:
                raise ValueError("非幂等或破坏性执行意图必须携带幂等键")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("执行意图幂等键不能为空白")
        return self


class ExecutionCapabilityEvidence(ExecutionContract):
    platform: PlatformKind
    provider: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    provider_version: str = Field(min_length=1, max_length=128)
    sandbox_levels: tuple[SandboxLevel, ...] = Field(min_length=1, max_length=3)
    network_modes: tuple[NetworkMode, ...] = Field(min_length=1, max_length=4)
    supports_pty: bool
    supports_background: bool
    supports_process_tree: bool
    evidence_digest: Revision

    @model_validator(mode="after")
    def unique_capabilities(self) -> Self:
        if len(set(self.sandbox_levels)) != len(self.sandbox_levels) or len(
            set(self.network_modes)
        ) != len(self.network_modes):
            raise ValueError("能力证据包含重复值")
        if self.sandbox_levels != tuple(sorted(self.sandbox_levels)) or self.network_modes != tuple(
            sorted(self.network_modes)
        ):
            raise ValueError("能力证据必须按规范顺序排列")
        if self.evidence_digest != capability_evidence_digest(self):
            raise ValueError("能力证据摘要不一致")
        return self


class ExecutionPlan(ExecutionContract):
    spec_version: Literal["harnessix.execution-plan/v1"] = "harnessix.execution-plan/v1"
    plan_id: UUID = Field(default_factory=uuid4)
    intent: ExecutionIntent
    workspace: WorkspaceSnapshot
    environment: tuple[EnvironmentBinding, ...] = Field(default=(), max_length=128)
    secrets: tuple[SecretVersionBinding, ...] = Field(default=(), max_length=32)
    sandbox: SandboxBinding
    policy: ExecutionPolicyBinding
    capabilities: ExecutionCapabilityEvidence
    fingerprint: Revision

    @field_validator("environment")
    @classmethod
    def unique_environment(
        cls, value: tuple[EnvironmentBinding, ...]
    ) -> tuple[EnvironmentBinding, ...]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("环境绑定必须唯一")
        return value

    @field_validator("secrets")
    @classmethod
    def unique_secrets(
        cls, value: tuple[SecretVersionBinding, ...]
    ) -> tuple[SecretVersionBinding, ...]:
        keys = [(item.name, item.target) for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("Secret绑定必须唯一")
        return value

    @model_validator(mode="after")
    def complete_binding(self) -> Self:
        comparison = str.casefold if self.workspace.platform == "windows" else lambda value: value
        environment_names = [comparison(item.name) for item in self.environment]
        secret_keys = [(item.name, comparison(item.target)) for item in self.secrets]
        secret_targets = [target for _, target in secret_keys]
        if environment_names != sorted(environment_names) or len(set(environment_names)) != len(
            environment_names
        ):
            raise ValueError("环境绑定不符合平台排序或唯一性")
        if secret_keys != sorted(secret_keys) or len(set(secret_targets)) != len(secret_targets):
            raise ValueError("Secret绑定不符合平台排序或目标唯一性")
        if set(environment_names) & set(secret_targets):
            raise ValueError("环境变量与Secret注入目标冲突")
        if self.sandbox.level not in self.capabilities.sandbox_levels:
            raise ValueError("Sandbox级别超出执行器能力")
        if self.sandbox.network not in self.capabilities.network_modes:
            raise ValueError("网络模式超出执行器能力")
        if self.sandbox.capability_digest != self.capabilities.evidence_digest:
            raise ValueError("Sandbox未绑定当前能力证据")
        if self.workspace.platform != self.capabilities.platform:
            raise ValueError("Workspace与执行器平台不一致")
        if self.fingerprint != execution_plan_fingerprint(self):
            raise ValueError("ExecutionPlan指纹不一致")
        return self


class SandboxBindingV2(SandboxBinding):
    profile_digest: Revision


class ExecutionCapabilityEvidenceV2(ExecutionCapabilityEvidence):
    spec_version: Literal["harnessix.execution-capability/v2"] = "harnessix.execution-capability/v2"
    provider_evidence_digest: Revision


class ExecutionPlanV2(ExecutionPlan):
    spec_version: Literal["harnessix.execution-plan/v2"] = "harnessix.execution-plan/v2"  # type: ignore[assignment]
    sandbox: SandboxBindingV2
    capabilities: ExecutionCapabilityEvidenceV2


class ExecutionApprovalCheckpoint(ExecutionContract):
    spec_version: Literal["harnessix.execution-approval/v1"] = "harnessix.execution-approval/v1"
    plan_id: UUID
    plan_fingerprint: Revision
    decision: ApprovalRecord

    @model_validator(mode="after")
    def decision_matches_plan(self) -> Self:
        if (
            self.decision.request_fingerprint != self.plan_fingerprint
            or not self.decision.actor.strip()
        ):
            raise ValueError("执行批准没有绑定完整计划")
        return self


def execution_plan_fingerprint(plan: ExecutionPlan) -> str:
    return canonical_digest(plan.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


def capability_evidence_digest(evidence: ExecutionCapabilityEvidence) -> str:
    return canonical_digest(
        evidence.model_dump(mode="json", exclude={"evidence_digest"}, warnings="error")
    )


def execution_is_approved(
    plan: ExecutionPlan | ExecutionPlanV2, checkpoint: ExecutionApprovalCheckpoint | None
) -> bool:
    if plan.policy.decision is PolicyDecisionKind.DENY:
        return False
    if plan.policy.decision is PolicyDecisionKind.ALLOW:
        return checkpoint is None
    return bool(
        checkpoint is not None
        and checkpoint.plan_id == plan.plan_id
        and checkpoint.plan_fingerprint == plan.fingerprint
        and checkpoint.decision.outcome is ApprovalOutcome.APPROVED
    )
