"""Historical Eval固定测试Profile的产品同源Trusted Action组合。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.test_contracts import RunTestsInput, TestProfile
from harnessix.processes.trusted_output import (
    TrustedProcessOutputDocument,
    build_trusted_process_output,
    trusted_process_public_output,
)
from harnessix.product_config.action_catalog import (
    ProductActionCatalog,
    ProductActionCatalogEntry,
)
from harnessix.product_config.action_contracts import (
    build_product_action_capability,
    build_product_action_capability_report,
    build_product_action_config,
)
from harnessix.product_config.eval_process import (
    VerifiedEvalTestProfile,
    _build_owner,
    _checked_arguments,
    _decode_run_tests,
    _descriptor,
    _process_spec,
    _resolved_action,
    _validate_route,
)
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore

_ACTION_OUTPUT_NAMESPACE = UUID("4c575932-3fba-4cc6-8bc8-7cdd84796b93")


async def _output_document(
    owner: VerifiedEvalTestProfile,
    plan_id: UUID,
) -> TrustedProcessOutputDocument:
    lease = owner.supervisor.status(plan_id)
    if lease.state not in {"exited", "failed", "unknown"}:
        raise KernelError("process_not_terminal", "Eval测试Process尚未形成终态")
    stdout, stderr = await asyncio.gather(
        owner.supervisor.output(plan_id, "stdout"),
        owner.supervisor.output(plan_id, "stderr"),
    )
    try:
        return build_trusted_process_output(owner.profile.name, lease, stdout, stderr)
    except ValueError:
        raise KernelError("process_output_corrupt", "Eval测试Process输出与Lease不一致") from None


def _public_output(document: TrustedProcessOutputDocument) -> dict[str, JsonValue]:
    return trusted_process_public_output(document, include_passed=True)


async def _lease_outcome(
    owner: VerifiedEvalTestProfile,
    lease: ProcessLease,
    *,
    origin: Literal["execution", "recovery"],
) -> ActionExecutionOutcome:
    try:
        document = await _output_document(owner, lease.process_id)
    except KernelError:
        return ActionExecutionOutcome(
            kind="unknown" if origin == "execution" else "manual_intervention",
            error_code="process_output_unavailable",
        )
    if lease.state == "unknown":
        kind: Literal["succeeded", "failed", "unknown", "manual_intervention"] = (
            "unknown" if origin == "execution" else "manual_intervention"
        )
        error_code = "process_state_unknown"
    elif lease.state == "failed":
        kind, error_code = "failed", "process_launch_failed"
    elif lease.stop_reason == "exited":
        # 退出码1是测试失败事实，不是测试执行基础设施失败；必须反馈给模型继续修复。
        kind, error_code = "succeeded", None
    else:
        kind, error_code = (
            "failed",
            {
                "timeout": "process_timeout",
                "cancelled": "process_cancelled",
                "output_limit": "process_output_limit",
                "input_limit": "process_input_limit",
                "io_error": "process_io_error",
                "closed": "process_closed",
                "cleanup_failed": "process_cleanup_failed",
                "host_lost": "process_host_lost",
                "unknown": "process_state_unknown",
                "launch_failed": "process_launch_failed",
            }.get(lease.stop_reason or "unknown", "process_failed"),
        )
    body = document.to_jsonl()
    return ActionExecutionOutcome(
        kind=kind,
        output=_public_output(document),
        artifact_sha256=hashlib.sha256(body).hexdigest(),
        error_code=error_code,
    )


async def _execution_failure(
    owner: VerifiedEvalTestProfile,
    plan_id: UUID,
    error: KernelError,
) -> ActionExecutionOutcome:
    try:
        lease = owner.supervisor.status(plan_id)
    except KernelError as missing:
        if missing.code != "process_lease_not_found":
            return ActionExecutionOutcome(kind="unknown", error_code="process_state_unavailable")
        deterministic = {
            "approval_required",
            "eval_action_workspace_mismatch",
            "eval_test_profile_plan_mismatch",
            "execution_plan_stale",
            "process_capability_mismatch",
            "secret_binding_mismatch",
            "workspace_snapshot_changed",
        }
        return ActionExecutionOutcome(
            kind="failed" if error.code in deterministic else "unknown",
            error_code=(
                "process_preflight_failed"
                if error.code in deterministic
                else "process_effect_unknown"
            ),
        )
    return await _lease_outcome(owner, lease, origin="execution")


class EvalRunTestsActionExecutor:
    """从批准的Profile选择确定性派生ProcessSpec并只经Supervisor执行。"""

    def __init__(self, owner: VerifiedEvalTestProfile, router: TrustedActionRouter) -> None:
        self.owner = owner
        self._router = router

    async def execute(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = _checked_arguments(arguments, self.owner)
        _validate_route(self.owner, route, checked)
        spec = _process_spec(self.owner, route)
        checkpoint = self._router.approval(route.execution.plan_id)
        try:
            lease = await self.owner.supervisor.run(
                route.execution,
                spec,
                self.owner.supervisor.capability,
                workspace=self.owner.environment.root,
                environment=self.owner.environment.environment,
                checkpoint=checkpoint,
                intent_arguments=route.invocation.arguments,
            )
        except asyncio.CancelledError:
            raise
        except KernelError as error:
            return await _execution_failure(self.owner, route.execution.plan_id, error)
        return await _lease_outcome(self.owner, lease, origin="execution")

    async def reconcile(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = _checked_arguments(arguments, self.owner)
        _validate_route(self.owner, route, checked)
        try:
            lease = await self.owner.supervisor.reconcile(route.execution.plan_id)
        except KernelError as error:
            if error.code == "process_lease_not_found":
                return ActionExecutionOutcome(kind="failed", error_code="process_not_started")
            return ActionExecutionOutcome(
                kind="manual_intervention",
                error_code="process_reconciliation_failed",
            )
        return await _lease_outcome(self.owner, lease, origin="recovery")

    async def output_document(self, plan_id: UUID) -> TrustedProcessOutputDocument:
        return await _output_document(self.owner, plan_id)


class EvalRunTestsOutputProvider:
    """从Supervisor Ledger重建正文并发布与Router摘要绑定的Action Output。"""

    def __init__(
        self,
        executor: EvalRunTestsActionExecutor,
        artifacts: SQLiteArtifactStore,
        *,
        workspace_scope: str,
    ) -> None:
        self._executor = executor
        self._artifacts = artifacts
        self._workspace_scope = workspace_scope

    async def output(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        *,
        expected_output_sha256: str,
        expected_artifact_sha256: str,
        cancel: CancelToken,
    ) -> JsonValue:
        cancel.checkpoint()
        document = await self._executor.output_document(route.plan.execution.plan_id)
        body = document.to_jsonl()
        public = _public_output(document)
        if (
            canonical_digest(public) != expected_output_sha256
            or hashlib.sha256(body).hexdigest() != expected_artifact_sha256
        ):
            raise KernelError("trusted_action_output_mismatch", "Eval测试输出与Router审计不一致")
        reference = await self._artifacts.publish_action_output(
            thread.thread_id,
            turn.turn_id,
            call,
            body,
            artifact_id=uuid5(_ACTION_OUTPUT_NAMESPACE, str(route.plan.execution.plan_id)),
            workspace_scope=self._workspace_scope,
            expected_sequence=thread.sequence,
        )
        return cast(JsonValue, {**public, "artifact": reference.model_dump(mode="json")})


def _definition(
    owner: VerifiedEvalTestProfile,
    router: TrustedActionRouter,
) -> tuple[TrustedActionDefinition, EvalRunTestsActionExecutor, ToolDescriptor]:
    descriptor = _descriptor(owner)
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=descriptor.effect_class,
        risk_level=descriptor.risk_level,
        recovery_mode="durable_ledger",
        executor_id="eval.run-tests",
    )
    executor = EvalRunTestsActionExecutor(owner, router)

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        return _decode_run_tests(owner.profile.name, arguments)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        return _resolved_action(owner, _checked_arguments(arguments, owner), context)

    definition = TrustedActionDefinition(
        binding=binding,
        input_model=RunTestsInput,
        resolve=resolve,
        executor=executor,
        input_schema=descriptor.input_schema,
        decode_arguments=decode,
    )
    return definition, executor, descriptor


@dataclass(slots=True)
class EvalTrustedActionComposition:
    """Eval进程内持有的Catalog、Gateway及三类持久执行账本。"""

    plans: SQLiteExecutionPlanStore
    audit: SQLiteActionAuditStore
    supervisor: PosixProcessSupervisor
    catalog: ProductActionCatalog
    gateway: RouterBackedAgentActionGateway
    _closed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gateway.close()
        await self.supervisor.aclose()
        self.audit.close()
        self.plans.close()


async def build_eval_trusted_action_composition(
    state_root: Path,
    workspace: Path,
    launcher: Path,
    profile: TestProfile,
    artifacts: SQLiteArtifactStore,
    *,
    workspace_scope: str,
) -> EvalTrustedActionComposition:
    """探测并原子安装run_tests Catalog、Router、Gateway和持久Owner。"""

    plans = SQLiteExecutionPlanStore(state_root / "execution-plans.db")
    audit = SQLiteActionAuditStore(state_root / "action-audit.db")
    supervisor: PosixProcessSupervisor | None = None
    try:
        supervisor = PosixProcessSupervisor(state_root / "process-state")
        owner = _build_owner(workspace, launcher, profile, supervisor)
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=owner.environment.workspace_root,
        )
        definition, executor, descriptor = _definition(owner, router)
        evidence = build_product_action_capability(
            capability_id=descriptor.name,
            kind="process_profile",
            status="verified",
            reason_code="verified",
            platform="posix",
            binding_digest=definition.binding.binding_digest,
            executor_evidence_digest=owner.executor_evidence_sha256,
        )
        report = build_product_action_capability_report(
            build_product_action_config(workspace_patch_enabled=False),
            (evidence,),
        )
        catalog = ProductActionCatalog(
            report,
            (
                ProductActionCatalogEntry(
                    description=descriptor.description,
                    definition=definition,
                    evidence=evidence,
                ),
            ),
        )
        catalog.install(router)
        gateway = RouterBackedAgentActionGateway(
            router,
            catalog.definitions(),
            lambda thread, _turn, _call: owner.environment.context(thread.workspace),
            presentations={descriptor.name: "process"},
            outputs={
                descriptor.name: EvalRunTestsOutputProvider(
                    executor,
                    artifacts,
                    workspace_scope=workspace_scope,
                )
            },
        )
        return EvalTrustedActionComposition(plans, audit, supervisor, catalog, gateway)
    except BaseException:
        if supervisor is not None:
            await supervisor.aclose()
        audit.close()
        plans.close()
        raise
