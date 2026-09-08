from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import (
    EnvironmentBinding,
    ExecutionCapabilityEvidence,
    ExecutionIntent,
    ExecutionPlan,
    ExecutionPolicyBinding,
    NetworkMode,
    SandboxBinding,
    SandboxLevel,
    SecretVersionBinding,
    canonical_digest,
    execution_plan_fingerprint,
)
from harnessix.workspace.contracts import PlatformKind, WorkspaceSnapshot


def build_capability_evidence(
    *,
    platform: PlatformKind,
    provider: str,
    provider_version: str,
    sandbox_levels: tuple[SandboxLevel, ...],
    network_modes: tuple[NetworkMode, ...],
    supports_pty: bool,
    supports_background: bool,
    supports_process_tree: bool,
) -> ExecutionCapabilityEvidence:
    payload = {
        "platform": platform,
        "provider": provider,
        "provider_version": provider_version,
        "sandbox_levels": list(sandbox_levels),
        "network_modes": list(network_modes),
        "supports_pty": supports_pty,
        "supports_background": supports_background,
        "supports_process_tree": supports_process_tree,
    }
    try:
        return ExecutionCapabilityEvidence(
            platform=platform,
            provider=provider,
            provider_version=provider_version,
            sandbox_levels=sandbox_levels,
            network_modes=network_modes,
            supports_pty=supports_pty,
            supports_background=supports_background,
            supports_process_tree=supports_process_tree,
            evidence_digest=canonical_digest(payload),
        )
    except ValidationError:
        raise KernelError("execution_capability_invalid", "执行器能力证据无效") from None


def bind_environment(
    values: Mapping[str, str], *, platform: PlatformKind
) -> tuple[EnvironmentBinding, ...]:
    if len(values) > 128:
        raise KernelError("execution_environment_denied", "执行环境变量超过上限")
    bindings: list[EnvironmentBinding] = []
    total = 0
    try:
        key = (lambda item: item[0].casefold()) if platform == "windows" else lambda item: item[0]
        items = sorted(values.items(), key=key)
        comparison_names = [name.casefold() if platform == "windows" else name for name, _ in items]
        if len(set(comparison_names)) != len(comparison_names):
            raise ValueError
        for name, value in items:
            if type(value) is not str or "\0" in value:
                raise ValueError
            encoded = value.encode("utf-8")
            total += len(name.encode()) + len(encoded) + 2
            bindings.append(
                EnvironmentBinding(
                    name=name,
                    value_sha256=hashlib.sha256(encoded).hexdigest(),
                )
            )
    except (UnicodeError, ValidationError, ValueError):
        raise KernelError("execution_environment_denied", "执行环境不符合契约") from None
    if total > 32768:
        raise KernelError("execution_environment_denied", "执行环境超过字节上限")
    return tuple(bindings)


def build_execution_plan(
    intent: ExecutionIntent,
    workspace: WorkspaceSnapshot,
    *,
    environment: Mapping[str, str],
    secrets: Sequence[SecretVersionBinding],
    sandbox: SandboxBinding,
    policy: ExecutionPolicyBinding,
    capabilities: ExecutionCapabilityEvidence,
    plan_id: UUID | None = None,
) -> ExecutionPlan:
    environment_binding = bind_environment(environment, platform=workspace.platform)
    secret_binding = tuple(
        sorted(
            secrets,
            key=lambda item: (
                item.name,
                item.target.casefold() if workspace.platform == "windows" else item.target,
            ),
        )
    )
    identifier = plan_id or uuid4()
    payload = {
        "spec_version": "harnessix.execution-plan/v1",
        "plan_id": str(identifier),
        "intent": intent.model_dump(mode="json", warnings="error"),
        "workspace": workspace.model_dump(mode="json", warnings="error"),
        "environment": [item.model_dump(mode="json") for item in environment_binding],
        "secrets": [item.model_dump(mode="json") for item in secret_binding],
        "sandbox": sandbox.model_dump(mode="json", warnings="error"),
        "policy": policy.model_dump(mode="json", warnings="error"),
        "capabilities": capabilities.model_dump(mode="json", warnings="error"),
    }
    fingerprint = canonical_digest(payload)
    try:
        return ExecutionPlan(
            plan_id=identifier,
            intent=intent,
            workspace=workspace,
            environment=environment_binding,
            secrets=secret_binding,
            sandbox=sandbox,
            policy=policy,
            capabilities=capabilities,
            fingerprint=fingerprint,
        )
    except ValidationError:
        raise KernelError("execution_plan_invalid", "执行计划绑定不符合契约") from None


def verify_execution_plan(
    plan: ExecutionPlan,
    *,
    intent: ExecutionIntent,
    workspace: WorkspaceSnapshot,
    environment: Mapping[str, str],
    secrets: Sequence[SecretVersionBinding],
    sandbox: SandboxBinding,
    policy: ExecutionPolicyBinding,
    capabilities: ExecutionCapabilityEvidence,
) -> None:
    try:
        checked = ExecutionPlan.model_validate_json(plan.model_dump_json(warnings="error"))
    except (ValidationError, ValueError):
        raise KernelError("execution_plan_mismatch", "执行计划自身校验失败") from None
    rebuilt = build_execution_plan(
        intent,
        workspace,
        environment=environment,
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
        plan_id=checked.plan_id,
    )
    if rebuilt != checked or execution_plan_fingerprint(checked) != checked.fingerprint:
        raise KernelError("execution_plan_stale", "执行计划绑定事实已经变化")
