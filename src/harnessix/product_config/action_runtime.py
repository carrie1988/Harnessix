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
from harnessix.product_config.action_owner import product_action_runtime_lock
from harnessix.product_config.action_recovery import scan_product_action_recovery
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.sandbox.process_runtime import ProcessSupervisor
from harnessix.secrets.provider import SecretProvider
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRouteSnapshot
from harnessix.trusted_actions.recovery_contracts import (
    ActionRecoveryScanReport,
    ActionRuntimeFence,
)
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore

_PRODUCT_ACTION_SOURCE = "harnessix.product"


@dataclass(frozen=True, slots=True)
class ProductActionRuntimeOwner:
    """统一持有一次产品启动的恢复结论、能力视图与Agent Gateway。"""

    composition: ProductActionComposition
    recovery: ProductActionStartupRecoveryReport
    fence: ActionRuntimeFence
    recovery_scan: ActionRecoveryScanReport

    @property
    def report(self) -> ProductActionCapabilityReport:
        return self.composition.report

    @property
    def catalog(self) -> ProductActionCatalog:
        return self.composition.catalog

    @property
    def gateway(self) -> RouterBackedAgentActionGateway | None:
        return self.composition.gateway


@dataclass(frozen=True, slots=True)
class _ProductActionDependencies:
    """一次产品启动内共享且按逆序关闭的Action基础设施。"""

    plans: SQLiteExecutionPlanStore
    audit: SQLiteActionAuditStore
    fence: ActionRuntimeFence
    transactions: SQLiteWorkspaceTransactionStore
    leases: WorkspaceLeaseStore
    supervisor: ProcessSupervisor | None
    probe_cache: dict[str, ProductProcessProfileProbeResult]


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


def _route_execute_timeout(*configs: ProductActionConfigV1) -> float:
    """Route期限覆盖固定Process期限及Owner清理余量，避免上层提前误判效果。"""

    return float(
        max(
            (
                300,
                *(
                    profile.timeout_seconds + 30
                    for config in configs
                    for profile in config.process_profiles
                ),
            )
        )
    )


@asynccontextmanager
async def _open_action_dependencies(
    state_root: Path,
    secrets: SecretProvider,
    *configs: ProductActionConfigV1,
) -> AsyncIterator[_ProductActionDependencies]:
    """在单Owner窗口内打开Store、Process Supervisor并冻结Profile探测。"""

    async with AsyncExitStack() as resources:
        plans = resources.enter_context(SQLiteExecutionPlanStore(state_root / "execution-plans.db"))
        audit = resources.enter_context(
            SQLiteActionAuditStore(
                state_root / "action-audit.db",
                require_runtime_owner=True,
            )
        )
        fence = resources.enter_context(audit.runtime_owner())
        transactions = resources.enter_context(
            SQLiteWorkspaceTransactionStore(state_root / "workspace-transactions")
        )
        leases = resources.enter_context(WorkspaceLeaseStore(state_root / "workspace-leases.db"))
        process_profiles = {
            profile.profile_sha256: profile
            for config in configs
            for profile in config.process_profiles
        }
        supervisor: ProcessSupervisor | None = None
        probe_cache: dict[str, ProductProcessProfileProbeResult] = {}
        if process_profiles:
            supervisor = await resources.enter_async_context(_process_supervisor(state_root))
            profiles = tuple(process_profiles.values())
            results = await asyncio.to_thread(
                _probe_process_profiles,
                profiles,
                supervisor,
                secrets,
            )
            probe_cache = dict(zip(process_profiles, results, strict=True))
        yield _ProductActionDependencies(
            plans,
            audit,
            fence,
            transactions,
            leases,
            supervisor,
            probe_cache,
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
    """持有全部Action资源，先初始化Session并结算旧Route，再发布候选目录。"""

    with product_action_runtime_lock(state_root):
        # 恢复扫描会读取Session与Artifact索引。调用方尚未打开AgentRuntime时，
        # Session Schema可能仍不存在；在统一组合根内幂等初始化，避免启动顺序隐式耦合。
        await artifacts.session.initialize()
        checked_config = ProductActionConfigV1.model_validate_json(
            action_config.model_dump_json(warnings="error")
        )
        checked_recovery = ProductActionConfigV1.model_validate_json(
            (recovery_config or checked_config).model_dump_json(warnings="error")
        )
        environment = build_fixed_product_action_environment(workspace_root)
        async with _open_action_dependencies(
            state_root,
            secrets,
            checked_recovery,
            checked_config,
        ) as dependencies:
            plans = dependencies.plans
            audit = dependencies.audit
            recovery_router = TrustedActionRouter(
                plans=plans,
                audit=audit,
                workspace_root=environment.workspace_root,
                execute_timeout_seconds=_route_execute_timeout(checked_recovery, checked_config),
            )
            recovery_composition = build_product_action_composition(
                checked_recovery,
                environment,
                recovery_router,
                dependencies.transactions,
                dependencies.leases,
                artifacts,
                artifact_workspace_scope=artifact_workspace_scope,
                process_probes=_select_probes(checked_recovery, dependencies.probe_cache),
                secrets=secrets,
            )
            recovery_scan = await scan_product_action_recovery(
                plans=plans,
                audit=audit,
                sessions=artifacts.session,
                artifacts=artifacts,
                supervisor=dependencies.supervisor,
                fence=dependencies.fence,
            )
            if (
                recovery_scan.invalid_execution_plans
                or recovery_scan.session_orphan_references
                or recovery_scan.process_orphan_leases
            ):
                raise KernelError(
                    "product_action_recovery_integrity",
                    "Product Action跨Store恢复扫描发现不可修复引用",
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
                    execute_timeout_seconds=_route_execute_timeout(checked_config),
                )
                composition = build_product_action_composition(
                    checked_config,
                    environment,
                    candidate_router,
                    dependencies.transactions,
                    dependencies.leases,
                    artifacts,
                    artifact_workspace_scope=artifact_workspace_scope,
                    process_probes=_select_probes(checked_config, dependencies.probe_cache),
                    secrets=secrets,
                )
                _verify_active_product_bindings(candidate_router, audit)
            yield ProductActionRuntimeOwner(
                composition,
                recovery_report,
                dependencies.fence,
                recovery_scan,
            )


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
