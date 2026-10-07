"""原 prepared 关联的阶段无关审批历史与材料交叉读取；不发布决定或执行效果。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.reducer import get_turn
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.execution.contracts import ExecutionApprovalCheckpoint
from harnessix.product_config.git_approval_history_projection import (
    OriginalGitApprovalHistory,
    interpret_git_approval_history,
)
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_materials import (
    read_product_git_delivery_core_materials_v2,
)
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_delivery_review_codec import (
    build_product_git_action_review,
    encode_product_git_action_review,
)
from harnessix.product_config.git_delivery_route_core import load_product_git_delivery_route_core_v2
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_proof import (
    PreparedLinkEvidence,
    _review_body,
    _source,
)
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.trusted_actions.contracts import ActionAuditEvent
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.terminal_read_control import require_terminal_read_scope


def _changed() -> KernelError:
    """固定语义拒绝与物理认证错误分开，正文不进入公开异常。"""
    return KernelError("git_approval_history_changed", "Git原审批历史无法核验")


@dataclass(frozen=True, slots=True)
class ApprovalHistoryEvidence:
    """同次私有读集合的原事实；不是批准发布、执行许可或可恢复授权。"""

    materials: PreparedLinkEvidence = field(repr=False)
    projection: OriginalGitApprovalHistory = field(repr=False)
    route_events: tuple[ActionAuditEvent, ...] = field(repr=False)
    approval: ExecutionApprovalCheckpoint | None = field(repr=False)
    approval_row: tuple[object, ...] | None = field(repr=False)


def _route_history(
    router: TrustedActionRouter, prepared: ProductGitPreparedLink, check: Callable[[], None]
) -> tuple[tuple[ActionAuditEvent, ...], ExecutionApprovalCheckpoint | None]:
    """原 Router 先验全 Hash 链，再冻结字段；检查点使用原 Execution 指纹域。"""
    check()
    route_id = prepared.plan.route.execution.plan_id
    events = _route_events(router, prepared, check)
    check()
    approval = router.approval(route_id)
    if approval is not None:
        approval = _snapshot(approval, ExecutionApprovalCheckpoint, check)
    check()
    return events, approval


def _route_events(
    router: TrustedActionRouter, prepared: ProductGitPreparedLink, check: Callable[[], None]
) -> tuple[ActionAuditEvent, ...]:
    """原 Audit Reader 验证完整链；终端作用域仍沿原父闭包控制。"""
    check()
    result = tuple(
        _snapshot(item, ActionAuditEvent, check)
        for item in router.events(prepared.plan.route.execution.plan_id)
    )
    check()
    return result


def _approval_row(
    router: TrustedActionRouter, prepared: ProductGitPreparedLink, check: Callable[[], None]
) -> tuple[object, ...] | None:
    """固定已经由原 load_approval 验证的物理行；不重解释或另造审批权威。"""
    check()
    row = router._plans._db.execute(
        "SELECT plan_id, plan_fingerprint, payload FROM execution_approvals WHERE plan_id=?",
        (str(prepared.plan.route.execution.plan_id),),
    ).fetchone()
    check()
    return cast(tuple[object, ...] | None, row)


def _require_live_request(
    history: AuthenticatedThreadHistory,
    prepared: ProductGitPreparedLink,
    projection: OriginalGitApprovalHistory,
    check: Callable[[], None],
) -> None:
    """历史决定时间有效不等于当前批准仍可用；原活跃请求不能刷新 Turn 期限。"""
    check()
    if (
        projection.state in {"pending", "approved"}
        and remaining_seconds(get_turn(history.thread, prepared.plan.core.turn_id)) <= 0
    ):
        raise _changed()
    check()


async def read_original_approval_evidence(
    prepared: ProductGitPreparedLink,
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    ports: WorkspaceSnapshotPorts,
    workspace_scope: str,
    *,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
) -> ApprovalHistoryEvidence:
    """输入必须来自同次全 Git MAC Row Reader；自身只读取原业务认证资源。"""
    check = checkpoint
    route_id = prepared.plan.route.execution.plan_id
    check()
    route = _snapshot(router.status(route_id, checkpoint=check), ActionRouteSnapshotV2, check)
    if route.plan != prepared.plan.route:
        raise _changed()
    core = load_product_git_delivery_route_core_v2(core_store, route.plan, checkpoint=check)
    publication = artifacts.session._publication
    assert publication is not None
    if core != prepared.plan.core or (
        (core.store_id, core.key_id) != publication.git_verifier.identity()
    ):
        raise _changed()
    history = await artifacts.session.authenticated_thread_history(
        core.thread_id, cancel=cancel, deadline=budget._deadline, checkpoint=check
    )
    events, approval = _route_history(router, prepared, check)
    approval_row = _approval_row(router, prepared, check)
    projection = interpret_git_approval_history(
        history, prepared, route, events, approval, checkpoint=check
    )
    _require_live_request(history, prepared, projection, check)
    _source(history, prepared, router, core_store, ports, check)
    materials = read_product_git_delivery_core_materials_v2(
        GitMaterialCAS(core_store.store), core, checkpoint=check
    )
    body = encode_product_git_action_review(
        build_product_git_action_review(core, materials.diff, checkpoint=check), checkpoint=check
    )
    await _review_body(artifacts, workspace_scope, prepared, body, check)
    terminal = await artifacts.session.authenticated_thread_history(
        core.thread_id, cancel=cancel, deadline=budget._deadline, checkpoint=check
    )
    check()
    if terminal != history or router.status(route_id, checkpoint=check) != route:
        raise _changed()
    if _route_history(router, prepared, check) != (events, approval) or (
        _approval_row(router, prepared, check) != approval_row
    ):
        raise _changed()
    if load_product_git_delivery_route_core_v2(core_store, route.plan, checkpoint=check) != core:
        raise _changed()
    _source(terminal, prepared, router, core_store, ports, check)
    _require_live_request(terminal, prepared, projection, check)
    return ApprovalHistoryEvidence(
        PreparedLinkEvidence(prepared, terminal, route, body),
        projection,
        events,
        approval,
        approval_row,
    )


def verify_original_approval_terminal(
    evidence: ApprovalHistoryEvidence,
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    check: Callable[[], None],
) -> None:
    """只在原终端读作用域调用；原四库无变化监视覆盖 Session 全历史与检查点。"""
    require_terminal_read_scope(core_store.store, router._audit)
    materials = evidence.materials
    # load_approval 会读 ExecutionPlanV3 并进入共享 _checkpoint；末端禁止该调用。
    # 原四库无变化监视与同次原严格验证后的物理全行比对共同保持原审批证明。
    if _route_events(router, materials.link, check) != evidence.route_events or (
        _approval_row(router, materials.link, check) != evidence.approval_row
    ):
        raise _changed()
    _require_live_request(materials.history, materials.link, evidence.projection, check)
    check()
