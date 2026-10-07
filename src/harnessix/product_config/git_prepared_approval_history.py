"""全原 prepared Git 行的审批历史只读 Reader；保留旧 pending Reader 的严格边界。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.git_approval_history_projection import OriginalGitApprovalHistory
from harnessix.product_config.git_approval_history_proof import (
    ApprovalHistoryEvidence,
    read_original_approval_evidence,
    verify_original_approval_terminal,
)
from harnessix.product_config.git_baseline import _BASELINE_TIMEOUT_SECONDS
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_prefix_rows import capture_git_prefix_rows
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_ledger import ProductGitPreparedLinkLedger, _control
from harnessix.product_config.git_prepared_link_observation import PreparedLinkReadSet
from harnessix.product_config.git_prepared_link_proof import prepared_link_changed
from harnessix.product_config.git_prepared_link_rows import read_prepared_link_rows
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.terminal_read_control import terminal_read_scope


@dataclass(frozen=True, slots=True)
class OriginalGitPreparedApprovalHistory:
    """过去 prepared 与当前原审批历史并列；已决定仍未发布 Git 决定或授权。"""

    prepared: ProductGitPreparedLink = field(repr=False)
    approval_history: OriginalGitApprovalHistory = field(repr=False)

    @property
    def linkage_state(self) -> Literal["prepared", "decision_not_linked"]:
        """区分仍待审批与决定尚未关联；两者都不是 Git approved 事件。"""
        return "prepared" if self.approval_history.state == "pending" else "decision_not_linked"


@dataclass(slots=True)
class _ApprovalReadSet(PreparedLinkReadSet):
    """原材料终端读集合的窄扩展；仅额外复核原审批检查点与完整 Route 链。"""

    approvals: dict[UUID, ApprovalHistoryEvidence] = field(default_factory=dict, repr=False)

    def terminal(
        self,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        artifacts: SQLiteArtifactStore,
        ports: WorkspaceSnapshotPorts,
        workspace_scope: str,
        check: Callable[[], None],
    ) -> None:
        """原材料/Review 复核不变；追加复核不进入共享构造回调或异步历史读取。"""
        super(_ApprovalReadSet, self).terminal(
            router, core_store, artifacts, ports, workspace_scope, check
        )
        with terminal_read_scope(
            core_store.store, router._audit, core_store.store._read_blob, check
        ):
            for evidence in self.approvals.values():
                verify_original_approval_terminal(evidence, router, core_store, check)
            check()


class ProductGitPreparedApprovalHistoryReader:
    """复用原 Ledger 控制窗口但不暴露其 prepare；仅读 sequence 0 全部关联。"""

    def __init__(
        self,
        database: sqlite3.Connection,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        artifacts: SQLiteArtifactStore,
        reader: GitReadRuntime,
        *,
        snapshot_ports: WorkspaceSnapshotPorts,
        workspace_scope: str,
    ) -> None:
        self._resources = ProductGitPreparedLinkLedger(
            database,
            router,
            core_store,
            artifacts,
            reader,
            snapshot_ports=snapshot_ports,
            workspace_scope=workspace_scope,
        )

    async def read_all(
        self, *, cancel: CancelToken, checkpoint: Callable[[], None]
    ) -> tuple[OriginalGitPreparedApprovalHistory, ...]:
        """完整只读回读：没有追加、恢复、审批、执行、对账或隐式提交。"""
        resources = self._resources
        await asyncio.sleep(0)
        budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
        read_set = _ApprovalReadSet()
        with _control(resources, cancel, budget, checkpoint, read_set) as check:
            timeout = asyncio.timeout(budget.remaining())
            try:
                async with timeout:
                    result = await _read_all(resources, cancel, budget, check, read_set)
                    check()
                    return result
            except TimeoutError:
                if timeout.expired():
                    raise KernelError("git_process_timeout", "Git审批历史总期限已耗尽") from None
                raise


async def _read_all(
    resources: ProductGitPreparedLinkLedger,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    read_set: _ApprovalReadSet,
) -> tuple[OriginalGitPreparedApprovalHistory, ...]:
    """全物理认证先于任何历史解释；任何关联错误拒绝整个集合。"""
    publication = resources._artifacts.session._publication
    assert publication is not None
    database = resources._database
    changes = database.total_changes
    anchor = database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    prepared, rows = read_prepared_link_rows(database, publication, checkpoint=check)
    result = []
    for link in prepared:
        evidence = await read_original_approval_evidence(
            link,
            resources._router,
            resources._core_store,
            resources._artifacts,
            resources._ports,
            resources._workspace_scope,
            cancel=cancel,
            budget=budget,
            checkpoint=check,
        )
        check()
        route_id = link.plan.route.execution.plan_id
        read_set.evidence[route_id] = evidence.materials
        read_set.approvals[route_id] = evidence
        result.append(OriginalGitPreparedApprovalHistory(link, evidence.projection))
    if (
        database.total_changes != changes
        or database.execute("SELECT * FROM git_prefix_anchor").fetchone() != anchor
        or capture_git_prefix_rows(database, checkpoint=check) != rows
    ):
        raise prepared_link_changed()
    read_set.complete(rows, anchor, changes)
    check()
    return tuple(result)
