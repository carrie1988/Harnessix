"""统一可信Action路由：绑定Tool、Execution Plan、Sandbox与Executor后执行Action。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

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
    SandboxBindingV2,
    SecretVersionBinding,
    ToolSourceKind,
    canonical_digest,
    execution_is_approved,
)
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
)
from harnessix.trusted_actions.planning import (
    canonical_json_object,
    decode_action_arguments,
    plan_action,
)
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import ResourceAccess, WorkspaceResourceRequest
from harnessix.workspace.snapshot import verify_workspace_snapshot


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
    """Trusted Action规划时绑定的Workspace、策略与能力上下文。"""

    workspace_root: Path
    sandbox: SandboxBindingV2
    capabilities: ExecutionCapabilityEvidenceV2
    cwd: str = "."
    environment: Mapping[str, str] = field(default_factory=dict)
    secrets: tuple[SecretVersionBinding, ...] = ()
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """已完成Tool解析、计划与Executor绑定的Action。"""

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
    """统一Action路由注册的Tool合同与Executor工厂。"""

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
        self.register_many((definition,))

    def register_many(self, definitions: Sequence[TrustedActionDefinition]) -> None:
        """先验证全部定义与冲突，再一次发布注册表，避免目录安装留下部分集合。"""

        checked = tuple(_validated_definition(item) for item in definitions)
        keys = [
            (item.binding.source, item.binding.source_id, item.binding.tool) for item in checked
        ]
        if len(keys) != len(set(keys)) or any(key in self._definitions for key in keys):
            raise KernelError("trusted_tool_duplicate", "Trusted Tool重复注册")
        updated = dict(self._definitions)
        updated.update(zip(keys, checked, strict=True))
        self._definitions = updated

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
        """冻结Tool合同、资源、策略、Sandbox与Executor身份，拒绝明文Secret和能力漂移。"""
        checked = CodingActionInvocation.model_validate_json(invocation.model_dump_json())
        definition = self._definition(checked.source, checked.source_id, checked.tool)
        return plan_action(
            checked,
            context,
            definition,
            policy=self._policy,
            plans=self._plans,
            audit=self._audit,
        )

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
        """Claim已批准计划并执行一次；取消后对账，发送后失败保留未知效果而不重试。"""
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
            arguments = decode_action_arguments(definition, plan.invocation.arguments)
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


def _validated_definition(definition: TrustedActionDefinition) -> TrustedActionDefinition:
    binding = TrustedToolBinding.model_validate_json(definition.binding.model_dump_json())
    if (definition.input_schema is None) != (definition.decode_arguments is None):
        raise KernelError(
            "trusted_tool_decoder_invalid",
            "显式Trusted Tool Schema必须同时提供参数解码器",
        )
    schema = (
        definition.input_model.model_json_schema()
        if definition.input_schema is None
        else canonical_json_object(definition.input_schema)
    )
    schema_digest = canonical_digest(schema)
    if schema_digest != binding.input_schema_sha256:
        raise KernelError("trusted_tool_schema_mismatch", "Trusted Tool输入Schema摘要不匹配")
    return TrustedActionDefinition(
        binding=binding,
        input_model=definition.input_model,
        resolve=definition.resolve,
        executor=definition.executor,
        input_schema=schema if definition.input_schema is not None else None,
        decode_arguments=definition.decode_arguments,
    )
