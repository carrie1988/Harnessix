"""从原认证 Session、Route、CAS 和完整 Review 形成待审批业务关联；不授予执行权。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemStatus, TrustedActionApprovalRequestContent, TurnStatus
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.artifacts.action_review_store import matching_action_review
from harnessix.artifacts.persistence import ARTIFACT_READ_SELECT
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    same_task_io_git_authentication,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
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
from harnessix.product_config.git_delivery_source import verify_git_delivery_source
from harnessix.product_config.git_native_control import git_checkpoint_boundary
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.sqlite_readonly import readonly_database
from harnessix.trusted_actions.agent_gateway_output import build_approval
from harnessix.trusted_actions.contracts import ActionRouteSnapshot
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.terminal_read_control import require_terminal_read_scope


def prepared_link_changed() -> KernelError:
    """业务错误不包含作者、消息、路径、对象正文或底层解析异常。"""
    return KernelError("git_prepared_link_changed", "Git待审批业务关联无法核验")


def _route_status(
    router: TrustedActionRouter, route_id: UUID, checkpoint: Callable[[], None]
) -> ActionRouteSnapshot:
    """原 Route 完整回读；仅父闭包计算分层，Store 观察与终端 Full 保持。"""
    if type(checkpoint) is not GitAuthenticationControl:
        return router.status(route_id, checkpoint=checkpoint)
    with git_checkpoint_boundary(checkpoint) as read_control:
        if type(read_control) is GitAuthenticationControl:
            return router.status(
                route_id,
                checkpoint=read_control,
                pure_progress=lambda: same_task_io_git_authentication(read_control),
            )
        return router.status(route_id, checkpoint=checkpoint)


@dataclass(frozen=True, slots=True)
class PreparedLinkEvidence:
    """同次已认证观察供末端同步复核；不暴露给模型，也不是执行授权。"""

    link: ProductGitPreparedLink
    history: AuthenticatedThreadHistory = field(repr=False)
    route: ActionRouteSnapshotV2 = field(repr=False)
    review_body: bytes = field(repr=False)


async def _review_body(
    artifacts: SQLiteArtifactStore,
    workspace_scope: str,
    link: ProductGitPreparedLink,
    expected: bytes,
    check: Callable[[], None],
) -> None:
    """原 MAC/唯一审批回指及全页完整正文逐字节比对，不重新发布或刷新 TTL。"""
    core, ref = link.plan.core, link.plan.review_artifact
    await artifacts.verify_reference(
        core.thread_id,
        core.call.call_id,
        ref,
        workspace_scope=workspace_scope,
        purpose="action_review",
        read_only=True,
    )
    check()
    offset = byte_offset = 0
    for _ in range(50):
        check()
        page = await artifacts.read(
            core.thread_id,
            workspace_scope,
            ref.artifact_id,
            offset=offset,
            limit=200,
            read_only=True,
        )
        check()
        body = page.text.encode("utf-8")
        if (
            page.artifact != ref
            or page.offset != offset
            or (body != expected[byte_offset : byte_offset + len(body)])
        ):
            raise prepared_link_changed()
        byte_offset += len(body)
        if page.next_offset is None:
            if byte_offset != len(expected):
                raise prepared_link_changed()
            return
        if page.next_offset <= offset:
            raise prepared_link_changed()
        offset = page.next_offset
    raise prepared_link_changed()


def _source(
    history: AuthenticatedThreadHistory,
    link: ProductGitPreparedLink,
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    ports: WorkspaceSnapshotPorts,
    check: Callable[[], None],
) -> None:
    """只读核验原成功 Patch 连续链与当前 Source2，不重新准备或覆盖用户代码。"""
    with git_checkpoint_boundary(check) as source_check:
        verify_git_delivery_source(
            history.thread,
            link.plan.core.baseline.source,
            router,
            core_store.store,
            checkpoint=source_check,
            snapshot_ports=ports,
        )


def _envelope(
    core: ProductGitDeliveryCoreV2,
    route: ActionRouteSnapshotV2,
    approval: TrustedActionApprovalRequestContent,
    check: Callable[[], None],
) -> ProductGitPreparedLink:
    """原 Plan2 唯一指纹算法，封套不另存第二份 Core、Route 或 Review 正文。"""
    ref = approval.diff_artifact
    if ref is None:
        raise prepared_link_changed()
    payload = {
        "spec_version": "harnessix.product-git-delivery-plan/v2",
        "core": core.model_dump(mode="json", warnings="error"),
        "route": route.plan.model_dump(mode="json", warnings="error"),
        "review_artifact": ref.model_dump(mode="json", warnings="error"),
    }
    plan = ProductGitDeliveryPlanV2(
        core=core,
        route=route.plan,
        review_artifact=ref,
        fingerprint=canonical_digest(payload),
    )
    return ProductGitPreparedLink.model_validate(
        {"plan": plan, "approval": approval}, context={"checkpoint": check}
    )


async def authenticate_prepared_link(
    route_id: UUID,
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    ports: WorkspaceSnapshotPorts,
    workspace_scope: str,
    *,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
) -> PreparedLinkEvidence:
    """只消费实际原认证资源；首末完整历史相等，不接纳调用方声明为签发来源。"""
    checkpoint()
    if type(route_id) is not UUID:
        raise prepared_link_changed()
    route = _snapshot(
        _route_status(router, route_id, checkpoint), ActionRouteSnapshotV2, checkpoint
    )
    if route.state != "pending_approval":
        raise prepared_link_changed()
    core = load_product_git_delivery_route_core_v2(core_store, route.plan, checkpoint=checkpoint)
    publication = artifacts.session._publication
    assert publication is not None
    if (core.store_id, core.key_id) != publication.git_verifier.identity():
        raise prepared_link_changed()
    history = await artifacts.session.authenticated_thread_history(
        core.thread_id, cancel=cancel, deadline=budget._deadline, checkpoint=checkpoint
    )
    checkpoint()
    turn = get_turn(history.thread, core.turn_id)
    calls = pending_calls(turn)
    # Review 生产发生于 executing_tools；本业务关联在原审批事件提交后才可形成。
    # 使用原 waiting_approval/首个 pending Call 语义，不能伪装成执行作用域。
    if history.thread.active_turn_id != core.turn_id or (
        turn.status is not TurnStatus.WAITING_APPROVAL or not calls or calls[0] != core.call
    ):
        raise prepared_link_changed()
    if trusted_action_invocation_id(core.thread_id, core.turn_id, core.call) != route_id:
        raise prepared_link_changed()
    approvals = [
        item
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
        and item.content.call_id == core.call.call_id
    ]
    if len(approvals) != 1 or approvals[0].status is not ItemStatus.STARTED:
        raise prepared_link_changed()
    approval = _snapshot(approvals[0].content, TrustedActionApprovalRequestContent, checkpoint)
    expected = build_approval(
        history.thread,
        turn,
        core.call,
        route,
        TrustedActionReview(diff_artifact=approval.diff_artifact),
        presentation="patch_batch",
    )
    if approval != expected:
        raise prepared_link_changed()
    link = _envelope(core, route, approval, checkpoint)
    _source(history, link, router, core_store, ports, checkpoint)
    materials = read_product_git_delivery_core_materials_v2(
        GitMaterialCAS(core_store.store), core, checkpoint=checkpoint
    )
    body = encode_product_git_action_review(
        build_product_git_action_review(core, materials.diff, checkpoint=checkpoint),
        checkpoint=checkpoint,
    )
    await _review_body(artifacts, workspace_scope, link, body, checkpoint)
    terminal = await artifacts.session.authenticated_thread_history(
        core.thread_id, cancel=cancel, deadline=budget._deadline, checkpoint=checkpoint
    )
    checkpoint()
    if terminal != history or _route_status(router, route_id, checkpoint) != route:
        raise prepared_link_changed()
    _source(terminal, link, router, core_store, ports, checkpoint)
    if (
        load_product_git_delivery_route_core_v2(core_store, route.plan, checkpoint=checkpoint)
        != core
    ):
        raise prepared_link_changed()
    checkpoint()
    return PreparedLinkEvidence(link, terminal, route, body)


def verify_prepared_link_terminal(
    evidence: PreparedLinkEvidence,
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    ports: WorkspaceSnapshotPorts,
    workspace_scope: str,
    *,
    checkpoint: Callable[[], None],
) -> None:
    """在全集同步读作用域内重验原 MAC、完整材料与业务状态，不进入共享 SDK 回调。"""
    require_terminal_read_scope(core_store.store, router._audit)
    link, core = evidence.link, evidence.link.plan.core
    checkpoint()
    if _route_status(router, link.plan.route.execution.plan_id, checkpoint) != evidence.route:
        raise prepared_link_changed()
    _source(evidence.history, link, router, core_store, ports, checkpoint)
    if (
        load_product_git_delivery_route_core_v2(core_store, link.plan.route, checkpoint=checkpoint)
        != core
    ):
        raise prepared_link_changed()
    materials = read_product_git_delivery_core_materials_v2(
        GitMaterialCAS(core_store.store), core, checkpoint=checkpoint
    )
    body = encode_product_git_action_review(
        build_product_git_action_review(core, materials.diff, checkpoint=checkpoint),
        checkpoint=checkpoint,
    )
    if body != evidence.review_body:
        raise prepared_link_changed()
    ref = link.plan.review_artifact
    try:
        database = readonly_database(artifacts.session.path)
    except sqlite3.Error:
        raise prepared_link_changed() from None
    try:
        database.row_factory = sqlite3.Row
        row = database.execute(
            ARTIFACT_READ_SELECT + " WHERE artifact_id=? AND thread_id=? AND workspace_scope=?",
            (str(ref.artifact_id), str(core.thread_id), workspace_scope),
        ).fetchone()
        if row is None:
            raise prepared_link_changed()
        actual = matching_action_review(
            row,
            thread_id=core.thread_id,
            turn_id=core.turn_id,
            call_id=core.call.call_id,
            artifact_id=ref.artifact_id,
            workspace_scope=workspace_scope,
            body=body,
            record_count=ref.records,
            publication=artifacts._publication,
        )
        if actual != ref or ref.expires_at <= utc_now():
            raise prepared_link_changed()
    except sqlite3.Error:
        raise prepared_link_changed() from None
    finally:
        database.close()
    checkpoint()
