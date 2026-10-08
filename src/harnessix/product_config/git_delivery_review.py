"""正式Git Review：原认证历史、全Core材料、原Artifact发布及唯一审批回指。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid5

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken, TurnCancelled, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.agent.publication import protect_json
from harnessix.agent.reducer import get_turn
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.domain.models import utc_now
from harnessix.product_config.git_baseline import _BASELINE_TIMEOUT_SECONDS
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
from harnessix.product_config.git_delivery_review_host import (
    _git_review_host_checks,
    _metadata_fields,
    _selected_fields_guard,
    require_git_review_host,
)
from harnessix.product_config.git_delivery_route_core import load_product_git_delivery_route_core_v2
from harnessix.product_config.git_delivery_source import verify_git_delivery_source
from harnessix.product_config.git_native_control import (
    native_git_checkpoint as _native_checkpointer,
)
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.contracts import ActionRouteSnapshot
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts

_NAMESPACE = UUID("41bb782b-9351-462c-a8a8-df2b98ea4ec0")
_BUDGET_IMPLEMENTATION = GitOperationBudget
_REVIEW_RESOURCE_FIELDS = (
    "_router",
    "_core_store",
    "_artifacts",
    "_reader",
    "_ports",
    "_workspace_scope",
)


def _changed() -> KernelError:
    """公开失败不包含作者消息、来源正文或底层数据库错误。"""
    return KernelError("git_action_review_changed", "Git审阅会话或原计划已经变化")


class ProductGitReviewProvider:
    """借原宿主生产完整审阅；产品默认Git注册与执行仍由正式装配负责。"""

    def __init__(
        self,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        artifacts: SQLiteArtifactStore,
        reader: GitReadRuntime,
        *,
        snapshot_ports: WorkspaceSnapshotPorts,
        workspace_scope: str,
    ) -> None:
        self._router, self._core_store = router, core_store
        self._artifacts, self._reader = artifacts, reader
        self._ports, self._workspace_scope = snapshot_ports, workspace_scope

    async def review(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> TrustedActionReview:
        """单次60秒覆盖全部阶段；取消、超时不重置期限或签发部分审阅。"""
        await asyncio.sleep(0)
        if type(cancel) is not CancelToken:
            raise KernelError("git_action_review_host_invalid", "Git审阅取消宿主无效")
        cancel.checkpoint()
        budget, local_check, check = _review_operation_controls(self, cancel)
        check()
        try:
            async with asyncio.timeout(budget.remaining()):
                result = await cancel.run(
                    _produce(
                        self,
                        route,
                        thread,
                        turn,
                        call,
                        cancel,
                        budget,
                        check,
                        local_check=local_check,
                    )
                )
                # 托管任务的finally也会await；公开交付前必须在父任务末端重新检查。
                check()
                if result.diff_artifact is None or result.diff_artifact.expires_at <= utc_now():
                    raise KernelError("artifact_expired", "Git审阅材料已经过期")
                return result
        except TimeoutError:
            raise KernelError("git_process_timeout", "Git审阅总期限已耗尽") from None


def _require_review_references(
    provider: ProductGitReviewProvider, references: tuple[object, ...]
) -> None:
    """full保留原动态引用读取顺序；纯metadata检查不调用本函数。"""
    current = (
        provider._router,
        provider._core_store,
        provider._artifacts,
        provider._reader,
        provider._ports,
        provider._workspace_scope,
    )
    if any(actual is not original for actual, original in zip(current, references, strict=True)):
        raise KernelError("git_action_review_host_invalid", "Git审阅原资源引用已经变化")


def _review_local_progress(cancel: CancelToken, budget: GitOperationBudget) -> Callable[[], None]:
    """同一取消/预算的原生读侧；实例shadow、非原生布尔/期限不能执行回调。"""
    code = "git_action_review_host_invalid"
    event = _metadata_fields(cancel, CancelToken, code).get("_event")
    if type(event) is not asyncio.Event:
        raise KernelError(code, "Git审阅原取消元数据已经变化")
    cancel_guard = _selected_fields_guard(
        cancel, CancelToken, ("_event", "checkpoint", "cancelled"), code
    )
    event_guard = _selected_fields_guard(event, asyncio.Event, ("is_set",), code)
    budget_guard = _selected_fields_guard(budget, _BUDGET_IMPLEMENTATION, ("remaining",), code)
    remaining = _BUDGET_IMPLEMENTATION.remaining

    def progress() -> None:
        cancel_guard()
        event_guard()
        event_fields = _metadata_fields(event, asyncio.Event, code)
        if type(event_fields.get("_value")) is not bool:
            raise KernelError(code, "Git审阅原取消元数据已经变化")
        if asyncio.Event.is_set(event):
            raise TurnCancelled
        budget_guard()
        budget_fields = _metadata_fields(budget, _BUDGET_IMPLEMENTATION, code)
        if type(budget_fields.get("_deadline")) is not float:
            raise KernelError(code, "Git审阅原期限元数据已经变化")
        remaining(budget)

    return progress


def _review_operation_controls(
    provider: ProductGitReviewProvider, cancel: CancelToken
) -> tuple[GitOperationBudget, Callable[[], None] | None, Callable[[], None]]:
    """在原父Task冻结资源与共同预算；只向exact受管生产者交付纯元数据频检。"""
    references = (
        provider._router,
        provider._core_store,
        provider._artifacts,
        provider._reader,
        provider._ports,
        provider._workspace_scope,
    )
    budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
    observer: Callable[[], None] | None = None
    if type(provider) is ProductGitReviewProvider:
        observer, host = _git_review_host_checks(*references)
    else:
        host = require_git_review_host(
            provider._router,
            provider._core_store,
            provider._artifacts,
            provider._reader,
            provider._ports,
            provider._workspace_scope,
        )

    def control() -> None:
        cancel.checkpoint()
        budget.remaining()
        host()
        _require_review_references(provider, references)

    check = parent_cancel_checkpointer(control)
    local_check: Callable[[], None] | None = None
    if observer is not None:
        local_host = observer
        progress = _review_local_progress(cancel, budget)
        resources = _selected_fields_guard(
            provider,
            ProductGitReviewProvider,
            _REVIEW_RESOURCE_FIELDS,
            "git_action_review_host_invalid",
        )

        def local() -> None:
            progress()
            local_host()
            resources()

        local_check = parent_cancel_checkpointer(local)
    return budget, local_check, check


async def _history(
    provider: ProductGitReviewProvider,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    route: ActionRouteSnapshotV2,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    expected: AuthenticatedThreadHistory | None = None,
) -> AuthenticatedThreadHistory:
    """三次均为实际认证完整重放；非认证模型外形或摘要不能替代原来源。"""
    history = await provider._artifacts.session.authenticated_thread_history(
        thread.thread_id, cancel=cancel, deadline=budget._deadline, checkpoint=check
    )
    check()
    if history.thread != thread or (expected is not None and history != expected):
        raise _changed()
    if get_turn(history.thread, turn.turn_id) != turn:
        raise _changed()
    ToolExecutionScope.for_pending_call(history.thread, turn.turn_id, call)
    actual = provider._router.status(
        trusted_action_invocation_id(thread.thread_id, turn.turn_id, call), checkpoint=check
    )
    if actual != route or route.state != "pending_approval":
        raise _changed()
    check()
    return history


async def _produce(
    provider: ProductGitReviewProvider,
    supplied: ActionRouteSnapshot,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    *,
    local_check: Callable[[], None] | None = None,
) -> TrustedActionReview:
    """原CAS完整恢复后只读复核来源；仅原Artifact发布允许新增持久状态。"""
    # 父闭包继续观察父取消；控制仅在本次cancel.run拥有的原子子Task创建。
    if type(provider) is ProductGitReviewProvider and local_check is not None:
        check = GitAuthenticationControl(local_check, check)
    route = _snapshot(supplied, ActionRouteSnapshotV2, check)
    thread, turn, call = (
        _snapshot(thread, Thread, check),
        _snapshot(turn, Turn, check),
        _snapshot(call, ToolCallContent, check),
    )
    history = await _history(provider, thread, turn, call, route, cancel, budget, check)
    core = load_product_git_delivery_route_core_v2(
        provider._core_store, route.plan, checkpoint=check
    )
    publication = provider._artifacts.session._publication
    assert publication is not None
    if (core.store_id, core.key_id, core.thread_id, core.turn_id, core.call) != (
        publication._store_id,
        publication._key_id,
        thread.thread_id,
        turn.turn_id,
        call,
    ):
        raise _changed()

    def source_check() -> None:
        try:
            verify_git_delivery_source(
                history.thread,
                core.baseline.source,
                provider._router,
                provider._core_store.store,
                checkpoint=_native_checkpointer(check),
                snapshot_ports=provider._ports,
            )
        except UpstreamCheckpointError as error:
            raise error.error from None

    source_check()
    materials = read_product_git_delivery_core_materials_v2(
        GitMaterialCAS(provider._core_store.store), core, checkpoint=check
    )
    # 先保护完整原文，避免同一密钥被正文切块边界分开后绕过逐记录扫描。
    await protect_json(publication._events._protection, materials.diff.content.text, cancel)
    check()
    body = encode_product_git_action_review(
        build_product_git_action_review(core, materials.diff, checkpoint=check), checkpoint=check
    )
    await _history(provider, thread, turn, call, route, cancel, budget, check, history)
    source_check()
    ref = await provider._artifacts.publish_action_review(
        thread.thread_id,
        turn.turn_id,
        call,
        body,
        artifact_id=uuid5(
            _NAMESPACE, f"{route.plan.execution.plan_id}:{core.fingerprint}:git-review:v1"
        ),
        workspace_scope=provider._workspace_scope,
        expected_sequence=thread.sequence,
    )
    check()
    await _history(provider, thread, turn, call, route, cancel, budget, check, history)
    source_check()
    check()
    return TrustedActionReview(diff_artifact=ref)
