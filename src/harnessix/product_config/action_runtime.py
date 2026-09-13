"""默认产品Workspace Patch组合的同步Store生命周期边界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_composition import (
    ProductWorkspacePatchComposition,
    build_fixed_product_action_environment,
    build_workspace_patch_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore


@contextmanager
def open_default_workspace_patch_runtime(
    state_root: Path,
    workspace_root: Path,
    artifacts: SQLiteArtifactStore,
    *,
    artifact_workspace_scope: str,
) -> Iterator[ProductWorkspacePatchComposition]:
    """打开Patch四个私有Store，并在全部构造成功后发布同源组合。"""

    environment = build_fixed_product_action_environment(workspace_root)
    with ExitStack() as stores:
        plans = stores.enter_context(SQLiteExecutionPlanStore(state_root / "execution-plans.db"))
        audit = stores.enter_context(SQLiteActionAuditStore(state_root / "action-audit.db"))
        transactions = stores.enter_context(
            SQLiteWorkspaceTransactionStore(state_root / "workspace-transactions")
        )
        leases = stores.enter_context(WorkspaceLeaseStore(state_root / "workspace-leases.db"))
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=environment.workspace_root,
        )
        yield build_workspace_patch_composition(
            build_product_action_config(),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=artifact_workspace_scope,
        )
