"""产品Trusted Action组合：从同一能力事实构造目录、Router与Agent Gateway。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.rollback_action import (
    WORKSPACE_ROLLBACK_TOOL,
    WorkspaceRollbackTransactionPlanner,
    build_workspace_rollback_definition,
    workspace_rollback_binding,
    workspace_rollback_descriptor,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action import (
    WORKSPACE_PATCH_TOOL,
    WorkspacePatchTransactionPlanner,
    build_workspace_patch_definition,
    workspace_patch_descriptor,
    workspace_patch_executor_evidence,
    workspace_patch_supported,
)
from harnessix.execution.contracts import (
    ExecutionCapabilityEvidenceV2,
    SandboxBindingV2,
    canonical_digest,
)
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.product_config.action_catalog import (
    ProductActionCatalog,
    ProductActionCatalogEntry,
)
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityEvidence,
    ProductActionCapabilityReport,
    ProductActionConfigV1,
    build_product_action_capability,
    build_product_action_capability_report,
)
from harnessix.product_config.process_action import (
    ProductProcessOutputProvider,
    build_product_process_definition,
)
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    VerifiedProductProcessProfile,
    process_tool_name,
)
from harnessix.product_config.workspace_patch_review import WorkspacePatchReviewProvider
from harnessix.product_config.workspace_rollback import (
    WorkspaceRollbackReviewProvider,
    authorize_workspace_rollback,
)
from harnessix.secrets.provider import SecretProvider
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.trusted_actions.agent_gateway import (
    RouterBackedAgentActionGateway,
    TrustedActionReviewProvider,
)
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter
from harnessix.workspace.contracts import PlatformKind
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot import capture_workspace_snapshot


@dataclass(frozen=True, slots=True)
class FixedProductActionEnvironment:
    """固定Workspace身份及本机执行能力；拒绝其他Thread根或漂移后的身份。"""

    root: Path
    workspace_id: str
    platform: PlatformKind
    capabilities: ExecutionCapabilityEvidenceV2
    sandbox: SandboxBindingV2

    def workspace_root(self, workspace_id: str) -> Path:
        if workspace_id != self.workspace_id:
            raise KernelError("product_workspace_mismatch", "Action计划不属于固定产品Workspace")
        return self.root

    def context(self, thread_workspace: str) -> ActionPlanningContext:
        try:
            selected = Path(thread_workspace).resolve(strict=True)
        except (OSError, RuntimeError):
            raise KernelError("product_workspace_mismatch", "Thread Workspace不可用") from None
        if selected != self.root:
            raise KernelError("product_workspace_mismatch", "Thread不属于固定产品Workspace")
        return ActionPlanningContext(
            workspace_root=self.root,
            sandbox=self.sandbox,
            capabilities=self.capabilities,
        )


@dataclass(frozen=True, slots=True)
class ProductActionComposition:
    """一次启动冻结的统一Action能力报告、目录和可选Agent Gateway。"""

    report: ProductActionCapabilityReport
    catalog: ProductActionCatalog
    gateway: RouterBackedAgentActionGateway | None


def build_fixed_product_action_environment(root: Path) -> FixedProductActionEnvironment:
    """探测本机平台并冻结固定Workspace身份、Host Guard与能力证据。"""

    platform: PlatformKind = "windows" if os.name == "nt" else "posix"
    snapshot = capture_workspace_snapshot(root, platform=platform)
    capabilities = build_capability_evidence_v2(
        platform=platform,
        provider="native-product-actions",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest(
            {
                "provider": "native-product-actions",
                "version": "1",
                "platform": platform,
            }
        ),
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest(
            {"profile": "product-workspace-patch", "platform": platform}
        ),
    )
    return FixedProductActionEnvironment(
        root=root.resolve(strict=True),
        workspace_id=snapshot.workspace_id,
        platform=platform,
        capabilities=capabilities,
        sandbox=sandbox,
    )


def _workspace_patch_component(
    config: ProductActionConfigV1,
    environment: FixedProductActionEnvironment,
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
) -> tuple[ProductActionCapabilityEvidence, ProductActionCatalogEntry | None]:
    reason = (
        "disabled"
        if not config.workspace_patch_enabled
        else "verified"
        if environment.platform == ("windows" if os.name == "nt" else "posix")
        and workspace_patch_supported(environment.root)
        else "platform_not_supported"
    )
    if reason != "verified":
        return (
            build_product_action_capability(
                capability_id=WORKSPACE_PATCH_TOOL,
                kind="workspace_patch",
                status="omitted",
                reason_code=reason,
                platform=environment.platform,
            ),
            None,
        )
    definition = build_workspace_patch_definition(
        transactions,
        leases,
        environment.workspace_root,
    )
    evidence = build_product_action_capability(
        capability_id=definition.binding.tool,
        kind="workspace_patch",
        status="verified",
        reason_code="verified",
        platform=environment.platform,
        binding_digest=definition.binding.binding_digest,
        executor_evidence_digest=workspace_patch_executor_evidence(),
    )
    return (
        evidence,
        ProductActionCatalogEntry(
            description=workspace_patch_descriptor().description,
            definition=definition,
            evidence=evidence,
        ),
    )


def _process_components(
    config: ProductActionConfigV1,
    probes: tuple[ProductProcessProfileProbeResult, ...],
    environment: FixedProductActionEnvironment,
    router: TrustedActionRouter,
    secrets: SecretProvider | None,
    artifacts: SQLiteArtifactStore,
    *,
    artifact_workspace_scope: str,
) -> tuple[
    tuple[ProductActionCapabilityEvidence, ...],
    tuple[ProductActionCatalogEntry, ...],
    dict[str, VerifiedProductProcessProfile],
    dict[str, ProductProcessOutputProvider],
]:
    """把每个Profile探测结果一次性收敛为省略事实或完整可执行组件。"""

    if tuple(item.profile for item in probes) != config.process_profiles:
        raise KernelError("product_process_probe_mismatch", "Process Profile探测集合与配置不一致")
    evidence: list[ProductActionCapabilityEvidence] = []
    entries: list[ProductActionCatalogEntry] = []
    owners: dict[str, VerifiedProductProcessProfile] = {}
    outputs: dict[str, ProductProcessOutputProvider] = {}
    for probe in probes:
        tool = process_tool_name(probe.profile.profile_id)
        if probe.verified is None:
            evidence.append(
                build_product_action_capability(
                    capability_id=tool,
                    kind="process_profile",
                    status="omitted",
                    reason_code=probe.reason_code,
                    platform=environment.platform,
                )
            )
            continue
        if secrets is None:
            raise KernelError(
                "product_process_probe_mismatch", "Process Profile缺少Secret Provider"
            )
        definition, executor, descriptor = build_product_process_definition(
            probe.verified,
            router,
            environment.workspace_root,
            secrets,
        )
        capability = build_product_action_capability(
            capability_id=tool,
            kind="process_profile",
            status="verified",
            reason_code="verified",
            platform=environment.platform,
            binding_digest=definition.binding.binding_digest,
            executor_evidence_digest=probe.verified.executor_evidence_sha256,
        )
        evidence.append(capability)
        entries.append(
            ProductActionCatalogEntry(
                description=descriptor.description,
                definition=definition,
                evidence=capability,
            )
        )
        owners[tool] = probe.verified
        outputs[tool] = ProductProcessOutputProvider(
            executor,
            artifacts,
            workspace_scope=artifact_workspace_scope,
        )
    return tuple(evidence), tuple(entries), owners, outputs


def _planning_context(
    environment: FixedProductActionEnvironment,
    process_owners: dict[str, VerifiedProductProcessProfile],
    thread_workspace: str,
    tool: str,
) -> ActionPlanningContext:
    base = environment.context(thread_workspace)
    owner = process_owners.get(tool)
    if owner is None:
        return base
    return ActionPlanningContext(
        workspace_root=base.workspace_root,
        sandbox=owner.sandbox,
        capabilities=owner.capabilities,
        environment=owner.environment,
        secrets=owner.secret_bindings,
    )


def _product_call_context(
    environment: FixedProductActionEnvironment,
    process_owners: dict[str, VerifiedProductProcessProfile],
    thread: Thread,
    call: ToolCallContent,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
) -> ActionPlanningContext:
    """只在正式产品入口验证回滚原会话归属，先于资源解析与私有Blob读取。"""
    base = _planning_context(environment, process_owners, thread.workspace, call.tool)
    if (
        call.tool in {WORKSPACE_PATCH_TOOL, WORKSPACE_ROLLBACK_TOOL}
        and router._snapshot_ports is not None
    ):
        base = replace(base, snapshot_ports=router._snapshot_ports)
    if call.tool == WORKSPACE_ROLLBACK_TOOL:
        authorize_workspace_rollback(thread, call, router, transactions)
    return base


def _rollback_component(
    patch: ProductActionCapabilityEvidence,
    environment: FixedProductActionEnvironment,
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
) -> tuple[ProductActionCapabilityEvidence, ProductActionCatalogEntry | None]:
    """原Patch门控制回滚广告、证据与可执行注册，不新增配置权限。"""

    rollback_binding = workspace_rollback_binding()
    rollback_evidence = build_product_action_capability(
        capability_id=WORKSPACE_ROLLBACK_TOOL,
        kind="workspace_patch",
        status=patch.status,
        reason_code=patch.reason_code,
        platform=environment.platform,
        binding_digest=rollback_binding.binding_digest if patch.status == "verified" else None,
        executor_evidence_digest=workspace_patch_executor_evidence()
        if patch.status == "verified"
        else None,
    )
    rollback_entry = (
        None
        if patch.status != "verified"
        else ProductActionCatalogEntry(
            description=workspace_rollback_descriptor().description,
            definition=build_workspace_rollback_definition(
                transactions, leases, environment.workspace_root
            ),
            evidence=rollback_evidence,
        )
    )
    return rollback_evidence, rollback_entry


def _compose_product_actions(
    config: ProductActionConfigV1,
    environment: FixedProductActionEnvironment,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
    artifacts: SQLiteArtifactStore,
    *,
    artifact_workspace_scope: str,
    process_probes: tuple[ProductProcessProfileProbeResult, ...] = (),
    secrets: SecretProvider | None = None,
    secret_scope: SecretPublicationScope | None = None,
) -> ProductActionComposition:
    """从统一配置和探测结果原子安装Patch及固定Container Process能力。"""

    patch_evidence, patch_entry = _workspace_patch_component(
        config,
        environment,
        transactions,
        leases,
    )
    rollback_evidence, rollback_entry = _rollback_component(
        patch_evidence, environment, transactions, leases
    )
    process_evidence, process_entries, process_owners, outputs = _process_components(
        config,
        process_probes,
        environment,
        router,
        secrets,
        artifacts,
        artifact_workspace_scope=artifact_workspace_scope,
    )
    evidence = tuple(
        sorted(
            (patch_evidence, rollback_evidence, *process_evidence),
            key=lambda item: item.capability_id,
        )
    )
    entries = tuple(
        sorted(
            (
                *(item for item in (patch_entry, rollback_entry) if item is not None),
                *process_entries,
            ),
            key=lambda item: item.evidence.capability_id,
        )
    )
    report = build_product_action_capability_report(config, evidence)
    catalog = ProductActionCatalog(report, entries)
    catalog.install(router)
    if not entries:
        return ProductActionComposition(report, catalog, None)

    presentations: dict[str, Literal["patch_batch", "process"]] = {
        tool: "process" for tool in process_owners
    }
    reviews: dict[str, TrustedActionReviewProvider] = {}
    if patch_entry is not None:
        presentations[WORKSPACE_PATCH_TOOL] = "patch_batch"
        reviews[WORKSPACE_PATCH_TOOL] = WorkspacePatchReviewProvider(
            WorkspacePatchTransactionPlanner(transactions, environment.workspace_root),
            artifacts,
            workspace_scope=artifact_workspace_scope,
        )
        presentations[WORKSPACE_ROLLBACK_TOOL] = "patch_batch"
        reviews[WORKSPACE_ROLLBACK_TOOL] = WorkspaceRollbackReviewProvider(
            WorkspaceRollbackTransactionPlanner(transactions, environment.workspace_root),
            artifacts,
            workspace_scope=artifact_workspace_scope,
        )
    gateway = RouterBackedAgentActionGateway(
        router,
        catalog.definitions(),
        lambda thread, _turn, call: _product_call_context(
            environment, process_owners, thread, call, router, transactions
        ),
        presentations=presentations,
        reviews=reviews,
        outputs=outputs,
        secret_scope=secret_scope,
        owns_secret_scope=True,
    )
    return ProductActionComposition(report, catalog, gateway)


def build_product_action_composition(
    config: ProductActionConfigV1,
    environment: FixedProductActionEnvironment,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
    artifacts: SQLiteArtifactStore,
    *,
    artifact_workspace_scope: str,
    process_probes: tuple[ProductProcessProfileProbeResult, ...] = (),
    secrets: SecretProvider | None = None,
) -> ProductActionComposition:
    """已验证Profile的执行与公开共用原版本快照；失败和Gateway关闭均释放材料。"""
    bindings = tuple(
        binding
        for probe in process_probes
        if probe.verified is not None
        for binding in probe.verified.secret_bindings
    )
    scope = SecretPublicationScope(bindings, secrets) if bindings and secrets is not None else None
    try:
        result = _compose_product_actions(
            config,
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=artifact_workspace_scope,
            process_probes=process_probes,
            secrets=scope if scope is not None else secrets,
            secret_scope=scope,
        )
        if result.gateway is None and scope is not None:
            scope.close()
        return result
    except BaseException:
        if scope is not None:
            scope.close()
        raise


def build_workspace_patch_composition(
    config: ProductActionConfigV1,
    environment: FixedProductActionEnvironment,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
    artifacts: SQLiteArtifactStore,
    *,
    artifact_workspace_scope: str,
) -> ProductActionComposition:
    """兼容现有Patch专项测试；正式产品统一使用build_product_action_composition。"""

    return build_product_action_composition(
        config,
        environment,
        router,
        transactions,
        leases,
        artifacts,
        artifact_workspace_scope=artifact_workspace_scope,
    )
