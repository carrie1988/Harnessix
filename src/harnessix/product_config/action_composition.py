"""产品Action组合：以已验证POSIX能力构造同源Patch目录、Gateway与Review。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
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
    ProductActionCapabilityReport,
    ProductActionConfigV1,
    build_product_action_capability,
    build_product_action_capability_report,
)
from harnessix.product_config.workspace_patch_review import WorkspacePatchReviewProvider
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
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
class ProductWorkspacePatchComposition:
    """一次启动冻结的Patch能力报告和可选Gateway。"""

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


def build_workspace_patch_composition(
    config: ProductActionConfigV1,
    environment: FixedProductActionEnvironment,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
    artifacts: SQLiteArtifactStore,
    *,
    artifact_workspace_scope: str,
) -> ProductWorkspacePatchComposition:
    """只有配置和本机POSIX证明同时成立时才原子安装Patch并构造Gateway。"""

    reason = (
        "disabled"
        if not config.workspace_patch_enabled
        else "verified"
        if environment.platform == "posix" and workspace_patch_supported()
        else "platform_not_supported"
    )
    if reason != "verified":
        evidence = build_product_action_capability(
            capability_id=WORKSPACE_PATCH_TOOL,
            kind="workspace_patch",
            status="omitted",
            reason_code=reason,
            platform=environment.platform,
        )
        report = build_product_action_capability_report(config, (evidence,))
        catalog = ProductActionCatalog(report, ())
        catalog.install(router)
        return ProductWorkspacePatchComposition(report, catalog, None)

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
    report = build_product_action_capability_report(config, (evidence,))
    catalog = ProductActionCatalog(
        report,
        (
            ProductActionCatalogEntry(
                description=workspace_patch_descriptor().description,
                definition=definition,
                evidence=evidence,
            ),
        ),
    )
    catalog.install(router)
    planner = WorkspacePatchTransactionPlanner(transactions, environment.workspace_root)
    review = WorkspacePatchReviewProvider(
        planner,
        artifacts,
        workspace_scope=artifact_workspace_scope,
    )
    gateway = RouterBackedAgentActionGateway(
        router,
        catalog.definitions(),
        lambda thread, _turn, _call: environment.context(thread.workspace),
        presentations={WORKSPACE_PATCH_TOOL: "patch_batch"},
        reviews=review,
    )
    return ProductWorkspacePatchComposition(report, catalog, gateway)
