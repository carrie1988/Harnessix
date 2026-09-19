"""默认产品Trusted Action Store、Process Owner与能力组合的统一生命周期。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import utc_now
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.product_config.action_catalog import ProductActionCatalog
from harnessix.product_config.action_composition import (
    ProductActionComposition,
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityReport,
    ProductActionConfigV1,
    ProductActionStartupRecoveryReport,
    ProductProcessProfile,
    product_action_startup_recovery_report_digest,
)
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.sandbox.process_runtime import ProcessSupervisor
from harnessix.secrets.provider import SecretProvider
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRouteSnapshot
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore

_PRODUCT_ACTION_SOURCE = "harnessix.product"


@dataclass(frozen=True, slots=True)
class ProductActionRuntimeOwner:
    """统一持有一次产品启动的恢复结论、能力视图与Agent Gateway。"""

    composition: ProductActionComposition
    recovery: ProductActionStartupRecoveryReport

    @property
    def report(self) -> ProductActionCapabilityReport:
        return self.composition.report

    @property
    def catalog(self) -> ProductActionCatalog:
        return self.composition.catalog

    @property
    def gateway(self) -> RouterBackedAgentActionGateway | None:
        return self.composition.gateway


def _process_supervisor(state_root: Path) -> ProcessSupervisor:
    """选择当前平台唯一受支持的Process Owner，不提供Host降级执行路径。"""

    if os.name == "nt":
        return WindowsProcessSupervisor(state_root / "process-owner")
    return PosixProcessSupervisor(state_root / "process-owner")


def _probe_process_profiles(
    profiles: tuple[ProductProcessProfile, ...],
    supervisor: ProcessSupervisor,
    secrets: SecretProvider,
) -> tuple[ProductProcessProfileProbeResult, ...]:
    return tuple(
        probe_product_process_profile(profile, supervisor, secrets) for profile in profiles
    )


@asynccontextmanager
async def open_default_product_action_runtime(
    state_root: Path,
    workspace_root: Path,
    artifacts: SQLiteArtifactStore,
    secrets: SecretProvider,
    action_config: ProductActionConfigV1,
    *,
    artifact_workspace_scope: str,
    recovery_config: ProductActionConfigV1 | None = None,
) -> AsyncIterator[ProductActionRuntimeOwner]:
    """持有全部Action资源，先结算旧Route，再发布候选目录。"""

    checked_config = ProductActionConfigV1.model_validate_json(
        action_config.model_dump_json(warnings="error")
    )
    checked_recovery = ProductActionConfigV1.model_validate_json(
        (recovery_config or checked_config).model_dump_json(warnings="error")
    )
    environment = build_fixed_product_action_environment(workspace_root)
    async with AsyncExitStack() as resources:
        plans = resources.enter_context(SQLiteExecutionPlanStore(state_root / "execution-plans.db"))
        audit = resources.enter_context(SQLiteActionAuditStore(state_root / "action-audit.db"))
        transactions = resources.enter_context(
            SQLiteWorkspaceTransactionStore(state_root / "workspace-transactions")
        )
        leases = resources.enter_context(WorkspaceLeaseStore(state_root / "workspace-leases.db"))
        recovery_router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=environment.workspace_root,
        )
        process_profiles = {
            profile.profile_sha256: profile
            for profile in (*checked_recovery.process_profiles, *checked_config.process_profiles)
        }
        probe_cache: dict[str, ProductProcessProfileProbeResult] = {}
        if process_profiles:
            supervisor = await resources.enter_async_context(_process_supervisor(state_root))
            profiles_to_probe = tuple(process_profiles.values())
            probe_cache = {
                digest: result
                for digest, result in zip(
                    process_profiles,
                    await asyncio.to_thread(
                        _probe_process_profiles,
                        profiles_to_probe,
                        supervisor,
                        secrets,
                    ),
                    strict=True,
                )
            }
        recovery_composition = build_product_action_composition(
            checked_recovery,
            environment,
            recovery_router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=artifact_workspace_scope,
            process_probes=_select_probes(checked_recovery, probe_cache),
            secrets=secrets,
        )
        recovery_report = await _recover_product_actions(
            recovery_router,
            audit,
            candidate_config_sha256=checked_config.config_sha256,
            recovery_config_sha256=checked_recovery.config_sha256,
        )
        if checked_recovery == checked_config:
            composition = recovery_composition
        else:
            candidate_router = TrustedActionRouter(
                plans=plans,
                audit=audit,
                workspace_root=environment.workspace_root,
            )
            composition = build_product_action_composition(
                checked_config,
                environment,
                candidate_router,
                transactions,
                leases,
                artifacts,
                artifact_workspace_scope=artifact_workspace_scope,
                process_probes=_select_probes(checked_config, probe_cache),
                secrets=secrets,
            )
            _verify_active_product_bindings(candidate_router, audit)
        yield ProductActionRuntimeOwner(composition, recovery_report)


def _select_probes(
    config: ProductActionConfigV1,
    cache: dict[str, ProductProcessProfileProbeResult],
) -> tuple[ProductProcessProfileProbeResult, ...]:
    return tuple(cache[profile.profile_sha256] for profile in config.process_profiles)


def _product_routes(audit: SQLiteActionAuditStore) -> tuple[ActionRouteSnapshot, ...]:
    return tuple(
        route
        for route in audit.active()
        if route.plan.binding.source == "builtin"
        and route.plan.binding.source_id == _PRODUCT_ACTION_SOURCE
    )


def _verify_active_product_bindings(
    router: TrustedActionRouter,
    audit: SQLiteActionAuditStore,
) -> None:
    registered = {
        (binding.source, binding.source_id, binding.tool): binding for binding in router.bindings()
    }
    for route in _product_routes(audit):
        binding = route.plan.binding
        if registered.get((binding.source, binding.source_id, binding.tool)) != binding:
            raise KernelError(
                "product_action_recovery_binding_unavailable",
                "在途Product Action缺少原始可信Binding",
            )


async def _recover_product_actions(
    router: TrustedActionRouter,
    audit: SQLiteActionAuditStore,
    *,
    candidate_config_sha256: str,
    recovery_config_sha256: str,
) -> ProductActionStartupRecoveryReport:
    """在协议开放前只核对产品Route；任何未知效果都不会重新执行。"""

    initial = _product_routes(audit)
    _verify_active_product_bindings(router, audit)
    interrupted = tuple(route for route in initial if route.state in {"running", "reconciling"})
    for route in interrupted:
        router.recover_interrupted_plan(route.plan.execution.plan_id)
    unknown = tuple(route for route in _product_routes(audit) if route.state == "unknown")
    outcomes: list[ActionExecutionOutcome] = []
    for route in unknown:
        outcomes.append(await router.reconcile(route.plan.execution.plan_id))
    remaining = _product_routes(audit)
    unresolved = sum(item.state in {"running", "unknown", "reconciling"} for item in remaining)
    pending = sum(item.state == "pending_approval" for item in remaining)
    ready = sum(item.state == "ready" for item in remaining)
    candidate = ProductActionStartupRecoveryReport.model_construct(
        _fields_set=None,
        candidate_config_sha256=candidate_config_sha256,
        recovery_config_sha256=recovery_config_sha256,
        scanned_routes=len(initial),
        interrupted_routes=len(interrupted),
        reconciled_routes=len(outcomes),
        succeeded_routes=sum(item.kind == "succeeded" for item in outcomes),
        failed_routes=sum(item.kind == "failed" for item in outcomes),
        manual_intervention_routes=sum(item.kind == "manual_intervention" for item in outcomes),
        unresolved_routes=sum(item.kind == "unknown" for item in outcomes),
        pending_approval_routes=pending,
        ready_routes=ready,
        created_at=utc_now(),
        report_sha256="0" * 64,
    )
    report = ProductActionStartupRecoveryReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=product_action_startup_recovery_report_digest(candidate),
    )
    if unresolved or report.unresolved_routes:
        raise KernelError(
            "product_action_recovery_incomplete", "Product Action启动恢复仍有未知效果"
        )
    return report
