from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    PolicyDecisionKind,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionCapabilityEvidenceV2,
    ExecutionIntent,
    SandboxBindingV2,
    SecretVersionBinding,
    ToolSourceKind,
    canonical_digest,
    execution_is_approved,
)
from harnessix.execution.planner import build_execution_plan_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.contracts import (
    ActionAuditEvent,
    ActionExecutionOutcome,
    ActionResourceAccess,
    ActionResourceKind,
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
from harnessix.workspace.contracts import ResourceAccess, WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot

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


def canonical_action_resource(
    *,
    kind: ActionResourceKind,
    access: ActionResourceAccess,
    identifier: object,
    attributes: object = (),
) -> CanonicalActionResource:
    try:
        return CanonicalActionResource(
            kind=kind,
            access=access,
            identifier_sha256=canonical_digest(identifier),
            attributes_sha256=canonical_digest(attributes),
        )
    except ValidationError:
        raise KernelError("action_resource_invalid", "Action规范资源无效") from None


@dataclass(frozen=True, slots=True)
class ActionPlanningContext:
    workspace_root: Path
    sandbox: SandboxBindingV2
    capabilities: ExecutionCapabilityEvidenceV2
    cwd: str = "."
    environment: Mapping[str, str] = field(default_factory=dict)
    secrets: tuple[SecretVersionBinding, ...] = ()
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    resources: tuple[CanonicalActionResource, ...]
    workspace_resources: tuple[WorkspaceResourceRequest, ...] = ()


class TrustedActionExecutor(Protocol):
    async def execute(
        self, plan: ActionRoutePlan, arguments: BaseModel
    ) -> ActionExecutionOutcome: ...

    async def reconcile(
        self, plan: ActionRoutePlan, arguments: BaseModel
    ) -> ActionExecutionOutcome: ...


ResourceResolver = Callable[[BaseModel, ActionPlanningContext], ResolvedAction]
ArgumentDecoder = Callable[[dict[str, JsonValue]], BaseModel]


@dataclass(frozen=True, slots=True)
class TrustedActionDefinition:
    binding: TrustedToolBinding
    input_model: type[BaseModel]
    resolve: ResourceResolver
    executor: TrustedActionExecutor
    input_schema: dict[str, JsonValue] | None = None
    decode_arguments: ArgumentDecoder | None = None


class TrustedActionRouter:
    """唯一计划、批准、执行和对账入口；注册信息全部由宿主持有。"""

    def __init__(
        self,
        *,
        plans: SQLiteExecutionPlanStore,
        audit: SQLiteActionAuditStore,
        workspace_root: Callable[[str], Path],
        policy: DefaultCodingRiskPolicy | None = None,
    ) -> None:
        self._plans = plans
        self._audit = audit
        self._workspace_root = workspace_root
        self._policy = policy or DefaultCodingRiskPolicy()
        self._definitions: dict[tuple[str, str, str], TrustedActionDefinition] = {}

    def register(self, definition: TrustedActionDefinition) -> None:
        binding = TrustedToolBinding.model_validate_json(definition.binding.model_dump_json())
        if (definition.input_schema is None) != (definition.decode_arguments is None):
            raise KernelError(
                "trusted_tool_decoder_invalid",
                "显式Trusted Tool Schema必须同时提供参数解码器",
            )
        schema = (
            definition.input_model.model_json_schema()
            if definition.input_schema is None
            else _canonical_json_object(definition.input_schema)
        )
        schema_digest = canonical_digest(schema)
        if schema_digest != binding.input_schema_sha256:
            raise KernelError("trusted_tool_schema_mismatch", "Trusted Tool输入Schema摘要不匹配")
        key = (binding.source, binding.source_id, binding.tool)
        if key in self._definitions:
            raise KernelError("trusted_tool_duplicate", "Trusted Tool重复注册")
        self._definitions[key] = TrustedActionDefinition(
            binding=binding,
            input_model=definition.input_model,
            resolve=definition.resolve,
            executor=definition.executor,
            input_schema=schema if definition.input_schema is not None else None,
            decode_arguments=definition.decode_arguments,
        )

    def bindings(
        self, *, source: str | None = None, source_id: str | None = None
    ) -> tuple[TrustedToolBinding, ...]:
        selected = (
            definition.binding.model_copy(deep=True)
            for definition in self._definitions.values()
            if (source is None or definition.binding.source == source)
            and (source_id is None or definition.binding.source_id == source_id)
        )
        return tuple(sorted(selected, key=lambda item: (item.source, item.source_id, item.tool)))

    def plan(
        self, invocation: CodingActionInvocation, context: ActionPlanningContext
    ) -> ActionRouteSnapshot:
        checked = CodingActionInvocation.model_validate_json(invocation.model_dump_json())
        definition = self._definition(checked.source, checked.source_id, checked.tool)
        binding = definition.binding
        if (
            checked.tool_version != binding.tool_version
            or checked.tool_fingerprint != binding.tool_fingerprint
        ):
            raise KernelError("trusted_tool_contract_changed", "调用的Trusted Tool契约已经变化")
        if _find_sensitive_path(checked.arguments) is not None:
            raise KernelError("raw_secret_rejected", "Action参数包含疑似明文凭据字段")
        try:
            arguments = _decode_action_arguments(definition, checked.arguments)
            dumped = arguments.model_dump(mode="json")
            if type(dumped) is not dict:
                raise ValueError
            normalized = cast(dict[str, JsonValue], dumped)
        except (ValidationError, ValueError, TypeError):
            raise KernelError(
                "tool_invalid_arguments", "Action参数不符合Trusted Tool契约"
            ) from None
        checked = checked.model_copy(update={"arguments": normalized})
        if (
            binding.effect_class in {EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.DESTRUCTIVE}
            and checked.idempotency_key is None
        ):
            raise KernelError("idempotency_key_required", "该Action必须携带幂等键")
        resolved = definition.resolve(arguments, context)
        resources = _canonical_resources(resolved.resources)
        policy = self._policy.evaluate(binding, resources, context.sandbox, context.secrets)
        snapshot = capture_workspace_snapshot(
            context.workspace_root,
            cwd=context.cwd,
            resources=resolved.workspace_resources,
            external_roots=context.external_roots,
            platform=context.capabilities.platform,
        )
        intent = ExecutionIntent(
            source=binding.source,
            source_id=binding.source_id,
            tool=binding.tool,
            tool_version=binding.tool_version,
            tool_fingerprint=binding.tool_fingerprint,
            arguments=checked.arguments,
            effect_class=binding.effect_class,
            risk_level=binding.risk_level,
            idempotency_key=checked.idempotency_key,
        )
        execution = build_execution_plan_v2(
            intent,
            snapshot,
            environment=context.environment,
            secrets=context.secrets,
            sandbox=context.sandbox,
            policy=policy,
            capabilities=context.capabilities,
            plan_id=checked.invocation_id,
        )
        external_action_id = (
            uuid5(_EXTERNAL_ACTION_NAMESPACE, f"{checked.invocation_id}:{binding.binding_digest}")
            if binding.recovery_mode == "external_reconcile"
            else None
        )
        identities = [
            (item.kind, item.access, item.identifier_sha256, item.attributes_sha256)
            for item in resources
        ]
        candidate = ActionRoutePlan.model_construct(
            _fields_set=None,
            invocation=checked,
            binding=binding,
            resources=resources,
            resources_sha256=canonical_digest(identities),
            execution=execution,
            external_action_id=external_action_id,
            fingerprint="0" * 64,
        )
        route = ActionRoutePlan(
            **candidate.model_dump(exclude={"fingerprint"}),
            fingerprint=action_route_plan_fingerprint(candidate),
        )
        self._plans.save_plan(execution)
        initial_state = cast(
            ActionRouteState,
            {
                PolicyDecisionKind.DENY: "denied",
                PolicyDecisionKind.REQUIRE_APPROVAL: "pending_approval",
                PolicyDecisionKind.ALLOW: "ready",
            }[policy.decision],
        )
        return self._audit.save_plan(route, initial_state=initial_state)

    def decide(self, plan_id: UUID, decision: ApprovalDecision) -> ActionRouteSnapshot:
        checked_decision = ApprovalDecision.model_validate_json(decision.model_dump_json())
        current = self._audit.load(plan_id)
        if current.plan.execution.policy.decision is not PolicyDecisionKind.REQUIRE_APPROVAL:
            raise KernelError("action_approval_not_required", "Action计划不接受人工审批")
        checkpoint = ExecutionApprovalCheckpoint(
            plan_id=plan_id,
            plan_fingerprint=current.plan.execution.fingerprint,
            decision=ApprovalRecord(
                outcome=checked_decision.outcome,
                actor=checked_decision.actor,
                reason=checked_decision.reason,
                request_fingerprint=current.plan.execution.fingerprint,
            ),
        )
        existing = self._plans.load_approval(plan_id)
        if existing is not None and existing != checkpoint:
            raise KernelError("approval_conflict", "Action计划已经绑定其他审批决定")
        if existing is None:
            self._plans.record_approval(checkpoint)
        target: ActionRouteState = (
            "ready" if checked_decision.outcome is ApprovalOutcome.APPROVED else "denied"
        )
        if current.state == target:
            return current
        return self._audit.transition(
            plan_id,
            expected={"pending_approval"},
            target=target,
            approval_outcome=checked_decision.outcome,
            approval_actor=checked_decision.actor,
            error_code=("approval_rejected" if target == "denied" else None),
        )

    async def execute(self, plan_id: UUID) -> ActionExecutionOutcome:
        current, definition, arguments = self._prepare_execution(plan_id)
        plan = current.plan
        self._audit.transition(
            plan_id,
            expected={"ready"},
            target="running",
            executor_id=plan.binding.executor_id,
            external_action_id=plan.external_action_id,
        )
        cancelled: asyncio.CancelledError | None = None
        try:
            outcome = await definition.executor.execute(plan, arguments)
            outcome = ActionExecutionOutcome.model_validate_json(outcome.model_dump_json())
            self._validate_outcome_identity(plan, outcome)
            if outcome.kind == "manual_intervention":
                raise KernelError("action_outcome_invalid", "首次执行不能直接进入人工处置终态")
        except asyncio.CancelledError as error:
            cancelled = error
            outcome = ActionExecutionOutcome(
                kind=(
                    "failed" if plan.binding.effect_class is EffectClass.READ_ONLY else "unknown"
                ),
                external_action_id=plan.external_action_id,
                error_code=(
                    "executor_cancelled"
                    if plan.binding.effect_class is EffectClass.READ_ONLY
                    else "cancelled_write_effect_unknown"
                ),
            )
        except UncertainEffectError:
            outcome = ActionExecutionOutcome(
                kind="unknown",
                external_action_id=plan.external_action_id,
                error_code="uncertain_external_effect",
            )
        except Exception:
            outcome = ActionExecutionOutcome(
                kind=(
                    "failed" if plan.binding.effect_class is EffectClass.READ_ONLY else "unknown"
                ),
                external_action_id=plan.external_action_id,
                error_code=(
                    "executor_error"
                    if plan.binding.effect_class is EffectClass.READ_ONLY
                    else "unexpected_write_error"
                ),
            )
        target = cast(ActionRouteState, outcome.kind)
        self._audit.transition(
            plan_id,
            expected={"running"},
            target=target,
            executor_id=plan.binding.executor_id,
            output_sha256=(
                canonical_digest(outcome.output) if outcome.output is not None else None
            ),
            artifact_sha256=outcome.artifact_sha256,
            external_action_id=outcome.external_action_id,
            error_code=outcome.error_code,
        )
        if cancelled is not None:
            raise cancelled
        return outcome

    async def reconcile(self, plan_id: UUID) -> ActionExecutionOutcome:
        current = self._audit.load(plan_id)
        plan = current.plan
        definition = self._matching_definition(plan)
        if plan.binding.recovery_mode == "none":
            outcome = ActionExecutionOutcome(
                kind="manual_intervention", error_code="reconciliation_not_supported"
            )
            self._audit.transition(
                plan_id,
                expected={"unknown"},
                target="manual_intervention",
                error_code=outcome.error_code,
                reconciliation=outcome.kind,
            )
            return outcome
        try:
            arguments = _decode_action_arguments(definition, plan.invocation.arguments)
        except (KernelError, ValidationError, ValueError, TypeError):
            raise KernelError("action_audit_store_corrupt", "持久Action参数不再可解析") from None
        self._audit.transition(
            plan_id,
            expected={"unknown"},
            target="reconciling",
            executor_id=plan.binding.executor_id,
            external_action_id=plan.external_action_id,
        )
        try:
            outcome = await definition.executor.reconcile(plan, arguments)
            outcome = ActionExecutionOutcome.model_validate_json(outcome.model_dump_json())
            self._validate_outcome_identity(plan, outcome)
        except Exception:
            outcome = ActionExecutionOutcome(
                kind="unknown",
                external_action_id=plan.external_action_id,
                error_code="reconciliation_error",
            )
        target = cast(ActionRouteState, outcome.kind)
        self._audit.transition(
            plan_id,
            expected={"reconciling"},
            target=target,
            executor_id=plan.binding.executor_id,
            output_sha256=(
                canonical_digest(outcome.output) if outcome.output is not None else None
            ),
            artifact_sha256=outcome.artifact_sha256,
            external_action_id=outcome.external_action_id,
            error_code=outcome.error_code,
            reconciliation=outcome.kind,
        )
        return outcome

    def recover_interrupted(self) -> tuple[UUID, ...]:
        recovered: list[UUID] = []
        for current in self._audit.active():
            if current.state not in {"running", "reconciling"}:
                continue
            self._audit.transition(
                current.plan.execution.plan_id,
                expected={current.state},
                target="unknown",
                executor_id=current.plan.binding.executor_id,
                external_action_id=current.plan.external_action_id,
                error_code="host_interrupted",
                reconciliation=("unknown" if current.state == "reconciling" else None),
            )
            recovered.append(current.plan.execution.plan_id)
        return tuple(recovered)

    def status(self, plan_id: UUID) -> ActionRouteSnapshot:
        return self._audit.load(plan_id)

    def events(self, plan_id: UUID) -> tuple[ActionAuditEvent, ...]:
        return self._audit.events(plan_id)

    def extension_port(
        self,
        *,
        source: str,
        source_id: str,
        context: Callable[[], ActionPlanningContext],
    ) -> ExtensionActionPort:
        if source not in {"mcp", "skill", "hook", "custom"}:
            raise KernelError("extension_source_invalid", "扩展端口来源类型无效")
        return ExtensionActionPort(self, source=source, source_id=source_id, context=context)

    def _prepare_execution(
        self, plan_id: UUID
    ) -> tuple[ActionRouteSnapshot, TrustedActionDefinition, BaseModel]:
        current = self._audit.load(plan_id)
        plan = current.plan
        persisted = self._plans.load_plan(plan_id)
        if persisted != plan.execution:
            raise KernelError("action_plan_mismatch", "Action Route与Execution Plan不一致")
        definition = self._matching_definition(plan)
        workspace_root = self._workspace_root(plan.execution.workspace.workspace_id)
        verify_workspace_snapshot(plan.execution.workspace, workspace_root)
        approval = self._plans.load_approval(plan_id)
        if not execution_is_approved(plan.execution, approval):
            raise KernelError("action_not_approved", "Action Execution Plan尚未获得有效批准")
        try:
            arguments = definition.input_model.model_validate_json(
                json.dumps(plan.invocation.arguments, ensure_ascii=False, allow_nan=False)
            )
        except ValidationError:
            raise KernelError("action_audit_store_corrupt", "持久Action参数不再可解析") from None
        return current, definition, arguments

    def _matching_definition(self, plan: ActionRoutePlan) -> TrustedActionDefinition:
        definition = self._definition(
            plan.binding.source, plan.binding.source_id, plan.binding.tool
        )
        if definition.binding != plan.binding:
            raise KernelError("trusted_tool_contract_changed", "执行前Trusted Tool绑定已经变化")
        return definition

    def _definition(self, source: str, source_id: str, tool: str) -> TrustedActionDefinition:
        try:
            return self._definitions[(source, source_id, tool)]
        except KeyError:
            raise KernelError(
                "trusted_tool_not_registered", "Tool未通过Trusted Action入口注册"
            ) from None

    @staticmethod
    def _validate_outcome_identity(plan: ActionRoutePlan, outcome: ActionExecutionOutcome) -> None:
        if outcome.external_action_id != plan.external_action_id:
            raise KernelError("external_action_identity_mismatch", "外部Action身份与计划不一致")


class ExtensionActionPort:
    """扩展只持有此能力端口，不获得executor、Session、Secret或文件系统对象。"""

    __slots__ = ("__context", "__router", "__source", "__source_id")

    def __init__(
        self,
        router: TrustedActionRouter,
        *,
        source: str,
        source_id: str,
        context: Callable[[], ActionPlanningContext],
    ) -> None:
        self.__router = router
        self.__source = source
        self.__source_id = source_id
        self.__context = context

    def bindings(self) -> tuple[TrustedToolBinding, ...]:
        return self.__router.bindings(source=self.__source, source_id=self.__source_id)

    def plan(
        self,
        *,
        invocation_id: UUID,
        tool: str,
        tool_version: str,
        tool_fingerprint: str,
        arguments: dict[str, JsonValue],
        idempotency_key: str | None = None,
    ) -> ActionRouteSnapshot:
        invocation = CodingActionInvocation(
            invocation_id=invocation_id,
            source=cast(ToolSourceKind, self.__source),
            source_id=self.__source_id,
            tool=tool,
            tool_version=tool_version,
            tool_fingerprint=tool_fingerprint,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )
        return self.__router.plan(invocation, self.__context())

    async def execute(self, plan_id: UUID) -> ActionExecutionOutcome:
        current = self.__router.status(plan_id)
        if (
            current.plan.binding.source != self.__source
            or current.plan.binding.source_id != self.__source_id
        ):
            raise KernelError("extension_plan_denied", "扩展不能使用其他来源的Action计划")
        return await self.__router.execute(plan_id)

    async def reconcile(self, plan_id: UUID) -> ActionExecutionOutcome:
        current = self.__router.status(plan_id)
        if (
            current.plan.binding.source != self.__source
            or current.plan.binding.source_id != self.__source_id
        ):
            raise KernelError("extension_plan_denied", "扩展不能核对其他来源的Action计划")
        return await self.__router.reconcile(plan_id)

    def status(self, plan_id: UUID) -> ActionRouteSnapshot:
        current = self.__router.status(plan_id)
        if (
            current.plan.binding.source != self.__source
            or current.plan.binding.source_id != self.__source_id
        ):
            raise KernelError("extension_plan_denied", "扩展不能读取其他来源的Action计划")
        return current


def _canonical_json_object(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        decoded = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise KernelError(
            "trusted_tool_schema_invalid", "Trusted Tool Schema不是规范JSON"
        ) from None
    if type(decoded) is not dict:
        raise KernelError("trusted_tool_schema_invalid", "Trusted Tool Schema必须是JSON对象")
    return cast(dict[str, JsonValue], decoded)


def _decode_action_arguments(
    definition: TrustedActionDefinition,
    arguments: Mapping[str, JsonValue],
) -> BaseModel:
    copied = _canonical_json_object(arguments)
    if definition.decode_arguments is not None:
        decoded = definition.decode_arguments(copied)
        if not isinstance(decoded, BaseModel):
            raise TypeError("Trusted Tool参数解码器必须返回BaseModel")
        return decoded
    return definition.input_model.model_validate_json(
        json.dumps(copied, ensure_ascii=False, allow_nan=False)
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
