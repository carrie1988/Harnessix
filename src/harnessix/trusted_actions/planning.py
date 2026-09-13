"""Trusted Action规划内核：规范化调用并持久化可恢复的确定性Route。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, PolicyDecisionKind
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPolicyBinding,
    canonical_digest,
)
from harnessix.execution.planner import build_execution_plan_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.contracts import (
    ActionRoutePlan,
    ActionRouteSnapshot,
    ActionRouteState,
    CanonicalActionResource,
    CodingActionInvocation,
    TrustedToolBinding,
    action_route_plan_fingerprint,
)
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.snapshot import capture_workspace_snapshot

if TYPE_CHECKING:
    from harnessix.trusted_actions.router import (
        ActionPlanningContext,
        TrustedActionDefinition,
    )

_EXTERNAL_ACTION_NAMESPACE = UUID("03e94e61-ab1c-4af5-b3da-46bf017b07b2")
_SENSITIVE_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "secret_key",
        "token",
    }
)


def plan_action(
    invocation: CodingActionInvocation,
    context: ActionPlanningContext,
    definition: TrustedActionDefinition,
    *,
    policy: DefaultCodingRiskPolicy,
    plans: SQLiteExecutionPlanStore,
    audit: SQLiteActionAuditStore,
) -> ActionRouteSnapshot:
    """冻结一次调用；重复身份只复用精确Route并修复跨Store崩溃窗口。"""

    checked, arguments = _normalize_invocation(invocation, definition)
    binding = definition.binding
    existing = _load_existing_route(audit, checked, binding)
    if existing is not None:
        # Audit持有完整Execution Plan，可修复首次规划在第二个Store写入前中断的窗口。
        plans.save_plan(existing.plan.execution)
        return existing
    if (
        binding.effect_class in {EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.DESTRUCTIVE}
        and checked.idempotency_key is None
    ):
        raise KernelError("idempotency_key_required", "该Action必须携带幂等键")
    resolved = definition.resolve(arguments, context)
    resources = _canonical_resources(resolved.resources)
    decision = policy.evaluate(binding, resources, context.sandbox, context.secrets)
    workspace = capture_workspace_snapshot(
        context.workspace_root,
        cwd=context.cwd,
        resources=resolved.workspace_resources,
        external_roots=context.external_roots,
        platform=context.capabilities.platform,
    )
    route = _build_route(checked, binding, resources, context, decision, workspace)
    initial_state = cast(
        ActionRouteState,
        {
            PolicyDecisionKind.DENY: "denied",
            PolicyDecisionKind.REQUIRE_APPROVAL: "pending_approval",
            PolicyDecisionKind.ALLOW: "ready",
        }[decision.decision],
    )
    snapshot = audit.save_plan(route, initial_state=initial_state)
    plans.save_plan(route.execution)
    return snapshot


def canonical_json_object(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """复制并规范化JSON对象，拒绝非有限数字、递归值和非对象结果。"""

    try:
        decoded = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise KernelError(
            "trusted_tool_schema_invalid", "Trusted Tool Schema不是规范JSON"
        ) from None
    if type(decoded) is not dict:
        raise KernelError("trusted_tool_schema_invalid", "Trusted Tool Schema必须是JSON对象")
    return cast(dict[str, JsonValue], decoded)


def decode_action_arguments(
    definition: TrustedActionDefinition,
    arguments: Mapping[str, JsonValue],
) -> BaseModel:
    """按注册时冻结的Pydantic合同或显式解码器解析参数。"""

    copied = canonical_json_object(arguments)
    if definition.decode_arguments is not None:
        decoded = definition.decode_arguments(copied)
        if not isinstance(decoded, BaseModel):
            raise TypeError("Trusted Tool参数解码器必须返回BaseModel")
        return decoded
    return definition.input_model.model_validate_json(
        json.dumps(copied, ensure_ascii=False, allow_nan=False)
    )


def _normalize_invocation(
    invocation: CodingActionInvocation,
    definition: TrustedActionDefinition,
) -> tuple[CodingActionInvocation, BaseModel]:
    binding = definition.binding
    if (
        invocation.tool_version != binding.tool_version
        or invocation.tool_fingerprint != binding.tool_fingerprint
    ):
        raise KernelError("trusted_tool_contract_changed", "调用的Trusted Tool契约已经变化")
    if _find_sensitive_path(invocation.arguments) is not None:
        raise KernelError("raw_secret_rejected", "Action参数包含疑似明文凭据字段")
    try:
        arguments = decode_action_arguments(definition, invocation.arguments)
        dumped = arguments.model_dump(mode="json")
        if type(dumped) is not dict:
            raise ValueError
        normalized = cast(dict[str, JsonValue], dumped)
    except (ValidationError, ValueError, TypeError):
        raise KernelError("tool_invalid_arguments", "Action参数不符合Trusted Tool契约") from None
    return invocation.model_copy(update={"arguments": normalized}), arguments


def _load_existing_route(
    audit: SQLiteActionAuditStore,
    invocation: CodingActionInvocation,
    binding: TrustedToolBinding,
) -> ActionRouteSnapshot | None:
    try:
        existing = audit.load(invocation.invocation_id)
    except KernelError as error:
        if error.code == "action_route_not_found":
            return None
        raise
    if existing.plan.invocation != invocation or existing.plan.binding != binding:
        raise KernelError("action_invocation_conflict", "Action调用标识已经绑定其他计划")
    return existing


def _build_route(
    invocation: CodingActionInvocation,
    binding: TrustedToolBinding,
    resources: tuple[CanonicalActionResource, ...],
    context: ActionPlanningContext,
    decision: ExecutionPolicyBinding,
    workspace: WorkspaceSnapshot,
) -> ActionRoutePlan:
    intent = ExecutionIntent(
        source=binding.source,
        source_id=binding.source_id,
        tool=binding.tool,
        tool_version=binding.tool_version,
        tool_fingerprint=binding.tool_fingerprint,
        arguments=invocation.arguments,
        effect_class=binding.effect_class,
        risk_level=binding.risk_level,
        idempotency_key=invocation.idempotency_key,
    )
    execution = build_execution_plan_v2(
        intent,
        workspace,
        environment=context.environment,
        secrets=context.secrets,
        sandbox=context.sandbox,
        policy=decision,
        capabilities=context.capabilities,
        plan_id=invocation.invocation_id,
    )
    external_action_id = (
        uuid5(_EXTERNAL_ACTION_NAMESPACE, f"{invocation.invocation_id}:{binding.binding_digest}")
        if binding.recovery_mode == "external_reconcile"
        else None
    )
    identities = [
        (item.kind, item.access, item.identifier_sha256, item.attributes_sha256)
        for item in resources
    ]
    candidate = ActionRoutePlan.model_construct(
        _fields_set=None,
        invocation=invocation,
        binding=binding,
        resources=resources,
        resources_sha256=canonical_digest(identities),
        execution=execution,
        external_action_id=external_action_id,
        fingerprint="0" * 64,
    )
    return ActionRoutePlan(
        **candidate.model_dump(exclude={"fingerprint"}),
        fingerprint=action_route_plan_fingerprint(candidate),
    )


def _canonical_resources(
    resources: Sequence[CanonicalActionResource],
) -> tuple[CanonicalActionResource, ...]:
    try:
        checked = tuple(
            CanonicalActionResource.model_validate_json(item.model_dump_json())
            for item in resources
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("action_resource_invalid", "Action规范资源无效") from None
    selected = tuple(
        sorted(
            checked,
            key=lambda item: (
                item.kind,
                item.access,
                item.identifier_sha256,
                item.attributes_sha256,
            ),
        )
    )
    if len(set(selected)) != len(selected):
        raise KernelError("action_resource_duplicate", "Action规范资源重复")
    return selected


def _find_sensitive_path(value: object, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).replace("-", "_")).lower()
            current = f"{path}.{key}" if path else str(key)
            if normalized in _SENSITIVE_KEYS or any(
                normalized.endswith(f"_{sensitive}") for sensitive in _SENSITIVE_KEYS
            ):
                return current
            found = _find_sensitive_path(child, current)
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            found = _find_sensitive_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None
