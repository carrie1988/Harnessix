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
from harnessix.product_config.git_decision_link_contracts import (
    ProductGitApprovedLink,
    ProductGitCancelledLink,
    ProductGitDeniedLink,
    snapshot_product_git_decision_link,
)
from harnessix.product_config.git_decision_link_rows import (
    GitLinkHistory,
    link_history_changed,
    read_git_link_history_rows,
)
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_prefix_rows import GitPrefixRows, capture_git_prefix_rows
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_ledger import (
    ProductGitPreparedLinkLedger,
    _control,
    _native_user_observer,
)
from harnessix.product_config.git_prepared_link_observation import PreparedLinkReadSet
from harnessix.product_config.git_prepared_link_proof import prepared_link_changed
from harnessix.product_config.git_prepared_link_rows import read_prepared_link_rows
from harnessix.product_config.git_user_observation import verify_product_git_user_observation
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


@dataclass(slots=True)
class _DecidedReadSet(_ApprovalReadSet):
    """新入口的返回绑定；末次外部回调后才比较完整来源，不签发能力。"""

    declaration: (
        tuple[UUID, ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink] | None
    ) = field(default=None, repr=False)

    validated_result: (
        ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink | None
    ) = field(default=None, repr=False)
    linked: tuple[GitLinkHistory, ...] = field(default=(), repr=False)

    def terminal(
        self,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        artifacts: SQLiteArtifactStore,
        ports: WorkspaceSnapshotPorts,
        workspace_scope: str,
        check: Callable[[], None],
    ) -> None:
        """原终端成功后以内部控制重建来源，并拒绝仅返回对象发生的漂移。"""
        super(_DecidedReadSet, self).terminal(
            router, core_store, artifacts, ports, workspace_scope, check
        )
        # 所有已存决定都须匹配同次原来源，包括未被请求的关联。
        for history in self.linked:
            check()
            if history.decision is not None:
                route_id = history.prepared.plan.route.execution.plan_id
                evidence = self.approvals.get(route_id)
                actual = snapshot_product_git_decision_link(history.decision, checkpoint=check)
                if evidence is None or actual != build_git_decision_link_sources(
                    evidence, checkpoint=check
                ):
                    raise link_history_changed()
        if self.declaration is None:
            raise KernelError("git_decision_source_changed", "Git决定返回来源发生变化")
        route_id, result = self.declaration
        evidence = self.approvals.get(route_id)
        if evidence is None:
            raise KernelError("git_decision_source_changed", "Git决定返回来源发生变化")
        expected = build_git_decision_link_sources(evidence, checkpoint=check)
        actual = snapshot_product_git_decision_link(result, checkpoint=check)
        if actual != expected:
            raise KernelError("git_decision_source_changed", "Git决定返回来源发生变化")
        check()
        self.validated_result = actual


class ProductGitPreparedApprovalHistoryReader:
    """复用原 Ledger 控制窗口，只读原审批及决定；不暴露 prepare 或发布能力。"""

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

    async def read_decided(
        self, route_id: UUID, *, cancel: CancelToken, checkpoint: Callable[[], None]
    ) -> ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink:
        """原资源中完整回读一个已决定事实；不签发 Token、发布决定或赋予写能力。"""
        if type(route_id) is not UUID:
            raise KernelError("git_decision_source_invalid", "Git决定来源定位无效")
        resources = self._resources
        await asyncio.sleep(0)
        budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
        read_set = _DecidedReadSet()
        with _control(resources, cancel, budget, checkpoint, read_set) as check:
            timeout = asyncio.timeout(budget.remaining())
            try:
                async with timeout:
                    # 全集认证不可退化为只验目标行；原 U/材料/Review/末端集合不变。
                    await _read_all(resources, cancel, budget, check, read_set)
                    evidence = read_set.approvals.get(route_id)
                    if evidence is None:
                        raise KernelError("git_decision_source_missing", "Git决定原关联不存在")
                    if evidence.projection.state == "pending":
                        raise KernelError("git_decision_source_pending", "Git原关联尚未决定")
                    result = build_git_decision_link_sources(evidence, checkpoint=check)
                    read_set.declaration = (route_id, result)
                    check()
                    # 不交付回调前求值的旧别名；只消费原终端实际核验的新快照。
            except TimeoutError:
                if timeout.expired():
                    raise KernelError("git_process_timeout", "Git决定来源总期限已耗尽") from None
                raise
        if read_set.validated_result is None:
            raise KernelError("git_decision_source_changed", "Git决定返回来源发生变化")
        return read_set.validated_result

    async def read_linked_decision(
        self, route_id: UUID, *, cancel: CancelToken, checkpoint: Callable[[], None]
    ) -> ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink:
        """回读已存 sequence 1 并核对全部原来源；缺失时不由原批准补造或补签。"""
        if type(route_id) is not UUID:
            raise KernelError("git_decision_source_invalid", "Git决定来源定位无效")
        resources = self._resources
        await asyncio.sleep(0)
        budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
        read_set = _DecidedReadSet()
        with _control(resources, cancel, budget, checkpoint, read_set) as check:
            timeout = asyncio.timeout(budget.remaining())
            try:
                async with timeout:
                    histories = await _read_linked_all(resources, cancel, budget, check, read_set)
                    decision = next(
                        (
                            history.decision
                            for history in histories
                            if history.prepared.plan.route.execution.plan_id == route_id
                        ),
                        None,
                    )
                    if decision is None:
                        raise KernelError("git_decision_link_missing", "Git原决定尚未关联")
                    read_set.linked = histories
                    read_set.declaration = (route_id, decision)
                    check()
            except TimeoutError:
                if timeout.expired():
                    raise KernelError("git_process_timeout", "Git决定关联总期限已耗尽") from None
                raise
        if read_set.validated_result is None:
            raise link_history_changed()
        return read_set.validated_result


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
        evidence = await _read_evidence(resources, link, cancel, budget, check, read_set)
        result.append(OriginalGitPreparedApprovalHistory(link, evidence.projection))
    _complete_read(resources, rows, anchor, changes, check, read_set)
    return tuple(result)


async def _read_evidence(
    resources: ProductGitPreparedLinkLedger,
    link: ProductGitPreparedLink,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    read_set: _ApprovalReadSet,
) -> ApprovalHistoryEvidence:
    """两种物理入口共用原审批、材料、Review、U 全集认证，不重捕获或准备。"""
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
    await verify_product_git_user_observation(
        link.plan.core.user_observation,
        evidence.materials.history,
        resources._router,
        resources._core_store.store,
        resources._reader,
        session=resources._artifacts.session,
        cancel=cancel,
        budget=budget,
        checkpoint=check,
        snapshot_ports=resources._ports,
        source_scope=read_set.source_scope,
        native_observer=_native_user_observer(resources, cancel, budget, check),
    )
    check()
    route_id = link.plan.route.execution.plan_id
    read_set.evidence[route_id] = evidence.materials
    read_set.approvals[route_id] = evidence
    return evidence


async def _read_linked_all(
    resources: ProductGitPreparedLinkLedger,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    read_set: _ApprovalReadSet,
) -> tuple[GitLinkHistory, ...]:
    """全部物理关联先验真，再逐关联核对原业务；坏的非目标决定同样拒绝。"""
    publication = resources._artifacts.session._publication
    assert publication is not None
    database = resources._database
    changes = database.total_changes
    anchor = database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    histories, rows = read_git_link_history_rows(database, publication, checkpoint=check)
    for history in histories:
        evidence = await _read_evidence(
            resources, history.prepared, cancel, budget, check, read_set
        )
        if history.decision is not None and history.decision != build_git_decision_link_sources(
            evidence, checkpoint=check
        ):
            raise link_history_changed()
    _complete_read(resources, rows, anchor, changes, check, read_set)
    return histories


def _complete_read(
    resources: ProductGitPreparedLinkLedger,
    rows: GitPrefixRows,
    anchor: tuple[object, ...] | None,
    changes: int,
    check: Callable[[], None],
    read_set: _ApprovalReadSet,
) -> None:
    """跨 await 后原全行、尾锚及同连接写计数必须不变，最后由原终端再次消费。"""
    database = resources._database
    if (
        database.total_changes != changes
        or database.execute("SELECT * FROM git_prefix_anchor").fetchone() != anchor
        or capture_git_prefix_rows(database, checkpoint=check) != rows
    ):
        raise prepared_link_changed()
    read_set.complete(rows, anchor, changes)
    check()
