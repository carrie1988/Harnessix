"""默认产品Trusted Action Store、Process Owner与能力组合的统一生命周期。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.product_config.action_composition import (
    ProductActionComposition,
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import ProductActionConfigV1
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.sandbox.process_runtime import ProcessSupervisor
from harnessix.secrets.provider import SecretProvider
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore


def _process_supervisor(state_root: Path) -> ProcessSupervisor:
    """选择当前平台唯一受支持的Process Owner，不提供Host降级执行路径。"""

    if os.name == "nt":
        return WindowsProcessSupervisor(state_root / "process-owner")
    return PosixProcessSupervisor(state_root / "process-owner")


def _probe_process_profiles(
    config: ProductActionConfigV1,
    supervisor: ProcessSupervisor,
    secrets: SecretProvider,
) -> tuple[ProductProcessProfileProbeResult, ...]:
    return tuple(
        probe_product_process_profile(profile, supervisor, secrets)
        for profile in config.process_profiles
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
) -> AsyncIterator[ProductActionComposition]:
    """持有统一Action Stores与可选Process Owner，并在完整探测后发布目录。"""

    checked_config = ProductActionConfigV1.model_validate_json(
        action_config.model_dump_json(warnings="error")
    )
    environment = build_fixed_product_action_environment(workspace_root)
    async with AsyncExitStack() as resources:
        plans = resources.enter_context(SQLiteExecutionPlanStore(state_root / "execution-plans.db"))
        audit = resources.enter_context(SQLiteActionAuditStore(state_root / "action-audit.db"))
        transactions = resources.enter_context(
            SQLiteWorkspaceTransactionStore(state_root / "workspace-transactions")
        )
        leases = resources.enter_context(WorkspaceLeaseStore(state_root / "workspace-leases.db"))
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=environment.workspace_root,
        )
        probes: tuple[ProductProcessProfileProbeResult, ...] = ()
        if checked_config.process_profiles:
            supervisor = await resources.enter_async_context(_process_supervisor(state_root))
            probes = await asyncio.to_thread(
                _probe_process_profiles,
                checked_config,
                supervisor,
                secrets,
            )
        yield build_product_action_composition(
            checked_config,
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=artifact_workspace_scope,
            process_probes=probes,
            secrets=secrets,
        )
