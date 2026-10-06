"""Trusted Action规划内核：规范化调用并持久化可恢复的确定性Route。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, PolicyDecisionKind
from harnessix.execution.contracts import (
    ExecutionIntent,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    canonical_digest,
)
from harnessix.execution.planner import build_execution_plan_v2, build_execution_plan_v3
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.tools.argument_feedback import invalid_argument_message
from harnessix.tools.workspace import ReadOperation
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
from harnessix.trusted_actions.public_errors import sanitize_plan_exception
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2

if TYPE_CHECKING:
    from harnessix.trusted_actions.router import (
        ActionPlanningContext,
        ResolvedAction,
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
    prepared: ResolvedAction | None = None,
) -> ActionRouteSnapshot:
    """冻结一次调用；重复身份只复用精确Route并修复跨Store崩溃窗口。"""

    checked, arguments = _normalize_invocation(invocation, definition)
    return _plan_normalized_action(
        checked,
        arguments,
        context,
        definition,
        policy=policy,
        plans=plans,
        audit=audit,
        prepared=prepared,
    )


def _plan_normalized_action(
    checked: CodingActionInvocation,
    arguments: BaseModel,
    context: ActionPlanningContext,
    definition: TrustedActionDefinition,
    *,
    policy: DefaultCodingRiskPolicy,
    plans: SQLiteExecutionPlanStore,
    audit: SQLiteActionAuditStore,
    prepared: ResolvedAction | None = None,
) -> ActionRouteSnapshot:
    """唯一规范化后的规划算法；异步准备后不得再次调用参数 Decoder。"""
    binding = definition.binding
    existing = _load_existing_route(audit, checked, binding)
    if existing is not None:
        # Audit持有完整Execution Plan，可修复首次规划在第二个Store写入前中断的窗口。
        plans.save_plan(existing.plan.execution)
        return existing
    if definition.agent_prepare is not None and prepared is None:
        raise KernelError("action_preparation_required", "该Action必须经Agent可信准备入口规划")
    if (
        binding.effect_class in {EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.DESTRUCTIVE}
        and checked.idempotency_key is None
    ):
        raise KernelError("idempotency_key_required", "该Action必须携带幂等键")
    try:
        resolved = prepared if prepared is not None else definition.resolve(arguments, context)
    except Exception as error:
        raise sanitize_plan_exception(error, stage="resolve") from None
    resources = _canonical_resources(resolved.resources)
    try:
        decision = policy.evaluate(binding, resources, context.sandbox, context.secrets)
    except Exception as error:
        raise sanitize_plan_exception(error, stage="policy") from None
    workspace: WorkspaceSnapshot | WorkspaceSnapshotV2
    if context.snapshot_ports is None:
        workspace = capture_workspace_snapshot(
            context.workspace_root,
            cwd=context.cwd,
            resources=resolved.workspace_resources,
            external_roots=context.external_roots,
            platform=context.capabilities.platform,
        )
    else:
        workspace = capture_workspace_snapshot_v2(
            context.workspace_root,
            cwd=context.cwd,
            resources=resolved.workspace_resources,
            external_roots=context.external_roots,
            platform=context.capabilities.platform,
            checkpoint=_planning_checkpoint(context),
            write_blob=context.snapshot_ports.write_blob,
            read_blob=context.snapshot_ports.read_blob,
        )
    if resolved.expected_workspace is not None and workspace != resolved.expected_workspace:
        raise KernelError(
            "action_preparation_workspace_changed", "可信准备后Workspace完整观察已变化"
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


def _planning_checkpoint(context: ActionPlanningContext) -> Callable[[], None]:
    """本次规划复用同一读取期限和上游Turn取消，不按父项重置。"""
    if context.checkpoint is None:
        return ReadOperation().checkpoint
    operation = NativeReadOperation(context.checkpoint)

    def check() -> None:
        try:
            operation.checkpoint()
        except UpstreamCheckpointError as error:
            raise error.error from None

    return check


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


def decode_persisted_action_arguments(
    definition: TrustedActionDefinition, invocation: CodingActionInvocation
) -> BaseModel:
    """持久输入使用原Decoder；任何解码不一致均归类为审计历史损坏。"""
    try:
        return decode_action_arguments(definition, invocation.arguments)
    except (KernelError, ValidationError, ValueError, TypeError):
        raise KernelError("action_audit_store_corrupt", "持久Action参数不再可解析") from None


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
        schema = (
            definition.input_schema
            if definition.input_schema is not None
            else definition.input_model.model_json_schema()
        )
        raise KernelError(
            "tool_invalid_arguments",
            invalid_argument_message(schema, invocation.arguments, trusted_action=True),
        ) from None
    except Exception as error:
        # 显式Decoder同样是回调边界，不能凭KernelError类型公开内部正文。
        raise sanitize_plan_exception(error, stage="decode") from None
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


def external_action_identity(
    invocation: CodingActionInvocation, binding: TrustedToolBinding
) -> UUID | None:
    """唯一原外部身份算法；供正式规划和完整 Core 恢复共同核对，不签发权限。"""
    return (
        uuid5(_EXTERNAL_ACTION_NAMESPACE, f"{invocation.invocation_id}:{binding.binding_digest}")
        if binding.recovery_mode == "external_reconcile"
        else None
    )


def _build_route(
    invocation: CodingActionInvocation,
    binding: TrustedToolBinding,
    resources: tuple[CanonicalActionResource, ...],
    context: ActionPlanningContext,
    decision: ExecutionPolicyBinding,
    workspace: WorkspaceSnapshot | WorkspaceSnapshotV2,
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
    execution: ExecutionPlanV2
    if isinstance(workspace, WorkspaceSnapshotV2):
        execution = build_execution_plan_v3(
            intent,
            workspace,
            environment=context.environment,
            secrets=context.secrets,
            sandbox=context.sandbox,
            policy=decision,
            capabilities=context.capabilities,
            plan_id=invocation.invocation_id,
        )
    else:
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
    external_action_id = external_action_identity(invocation, binding)
    identities = [
        (item.kind, item.access, item.identifier_sha256, item.attributes_sha256)
        for item in resources
    ]
    route_type = (
        ActionRoutePlanV2 if isinstance(workspace, WorkspaceSnapshotV2) else ActionRoutePlan
    )
    candidate = route_type.model_construct(
        _fields_set=None,
        invocation=invocation,
        binding=binding,
        resources=resources,
        resources_sha256=canonical_digest(identities),
        execution=execution,
        external_action_id=external_action_id,
        fingerprint="0" * 64,
    )
    return route_type(
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
