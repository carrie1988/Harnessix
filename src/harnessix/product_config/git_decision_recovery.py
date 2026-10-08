"""内部决定事实恢复编排；不注册默认调度、不调用 Git execute/reconcile。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Turn, TurnStatus
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.agent.runtime import AgentRuntime
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config.git_baseline import _BASELINE_TIMEOUT_SECONDS
from harnessix.product_config.git_decision_link_contracts import ProductGitDecisionLink
from harnessix.product_config.git_decision_link_ledger import ProductGitDecisionLinkLedger
from harnessix.product_config.git_decision_link_rows import read_git_link_history_rows
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryCoreV2
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_delivery_review_host import require_git_review_host
from harnessix.product_config.git_delivery_route_core import load_product_git_delivery_route_core_v2
from harnessix.product_config.git_prefix_sql import (
    _git_prefix_caller_transaction_epoch,
    _git_prefix_caller_transaction_observer,
    _require_git_prefix_caller_transaction_epoch,
    git_prefix_sql_window,
)
from harnessix.product_config.git_prepared_link_connection import (
    _prepared_git_connection_observer,
    open_prepared_git_connection,
)
from harnessix.product_config.git_prepared_link_ledger import ProductGitPreparedLinkLedger
from harnessix.product_config.git_prepared_runtime_thread import (
    _prepared_runtime_thread_observer,
    _require_scope,
    bind_prepared_git_runtime_thread,
    prepared_git_commit_scope,
    require_prepared_git_runtime_thread,
)
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2


def _invalid() -> KernelError:
    return KernelError("git_decision_recovery_scope_invalid", "Git原决定恢复窗口无效")


def _require_original_prepared_target(
    resources: ProductGitPreparedLinkLedger,
    runtime: AgentRuntime,
    route: ActionRouteSnapshotV2,
    core: ProductGitDeliveryCoreV2,
    check: Callable[[], None],
) -> None:
    """同锁下先认原 prepared 目标；Router-first 不满足完整决定 Reader 的前置条件。"""
    path = resources._artifacts.session.path.parent / "git-delivery/git-delivery.db"
    publication = resources._artifacts.session._publication
    assert publication is not None
    # 私有 mode=ro 短事务只认证前缀，factory close 统一回收；不代管原写连接的事务。
    with open_prepared_git_connection(path, read_only=True, checkpoint=check) as source:
        with bind_prepared_git_runtime_thread(source, runtime, core.thread_id):
            source.execute("BEGIN")
            with git_prefix_sql_window(source, checkpoint=check):
                histories, _rows = read_git_link_history_rows(source, publication, checkpoint=check)
                target = next(
                    (
                        h.prepared
                        for h in histories
                        if h.prepared.plan.route.execution.plan_id == route.plan.execution.plan_id
                    ),
                    None,
                )
                if target is None or target.plan.core != core or target.plan.route != route.plan:
                    raise _invalid()
                check()


@dataclass(slots=True)
class _RecoveryProgress:
    """本次恢复的可变阶段，不是可转交、可持久化的授权见证。"""

    turn: Turn | None = None
    epoch: tuple[object, int] | None = None
    observe_transaction: Callable[[], None] | None = None


def _recovery_control(
    ledger: ProductGitDecisionLinkLedger,
    cancel: CancelToken,
    checkpoint: Callable[[], None],
    progress: _RecoveryProgress,
) -> tuple[GitOperationBudget, GitAuthenticationControl, Callable[[], None]]:
    """冻结原资源；局部频检与完整来源认证仍分别由原操作消费。"""
    resources = ledger._resources
    database = resources._database
    scope = _require_scope(database)
    runtime = scope.runtime
    observe_runtime = _prepared_runtime_thread_observer(
        database, resources._router, resources._artifacts
    )
    # 原宿主先准入，再拒绝调用方已有事务；复合失效也保留原首异常顺序。
    if database.in_transaction:
        raise _invalid()
    budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
    references = tuple(vars(resources).values())
    host = require_git_review_host(
        resources._router,
        resources._core_store,
        resources._artifacts,
        resources._reader,
        resources._ports,
        resources._workspace_scope,
    )
    connection = _prepared_git_connection_observer(
        database, resources._artifacts.session.path.parent / "git-delivery/git-delivery.db"
    )

    def local() -> None:
        cancel.checkpoint()
        budget.remaining()
        if progress.turn is not None and remaining_seconds(progress.turn) <= 0:
            raise KernelError("git_process_timeout", "Git恢复的原 Turn 期限已耗尽")
        observe_runtime()
        if ledger._resources is not resources or scope.runtime is not runtime:
            raise _invalid()
        if any(a is not b for a, b in zip(vars(resources).values(), references, strict=True)):
            raise _invalid()
        if progress.epoch is None:
            if database.in_transaction:
                raise _invalid()
        else:
            assert progress.observe_transaction is not None
            progress.observe_transaction()

    local = parent_cancel_checkpointer(local)

    def authenticate() -> None:
        local()
        host()
        connection()
        checkpoint()
        local()
        host()
        connection()

    control = GitAuthenticationControl(local, authenticate)

    def caller_checkpoint() -> None:
        # 原 Ledger 自身在 I/O／发布边界前后执行完整认证，无需重复该算法。
        local()
        checkpoint()
        local()

    return budget, control, caller_checkpoint


def _limit_to_original_turn(
    core: ProductGitDeliveryCoreV2, turn: Turn, budget: GitOperationBudget, timeout: asyncio.Timeout
) -> None:
    """只收紧本次总期限；首个待处理 Call 必须仍是该原 Core 的调用。"""
    if turn.status is TurnStatus.WAITING_APPROVAL:
        calls = pending_calls(turn)
        if not calls or calls[0] != core.call:
            raise _invalid()
    remaining = min(budget.remaining(), remaining_seconds(turn))
    original_deadline = timeout.when()
    assert original_deadline is not None
    timeout.reschedule(min(original_deadline, asyncio.get_running_loop().time() + remaining))
    # 同一对象只缩短；Ledger 的纯段、终端及 COMMIT 都消费这一绝对期限。
    budget._deadline = min(budget._deadline, time.monotonic() + remaining)


async def recover_decision_link(
    ledger: ProductGitDecisionLinkLedger,
    route_id: UUID,
    *,
    cancel: CancelToken,
    checkpoint: Callable[[], None],
) -> ProductGitDecisionLink:
    """原 Session 同步成功后才开启本事务；事实提交不授予任何效果执行权。"""
    await asyncio.sleep(0)
    if type(ledger) is not ProductGitDecisionLinkLedger or type(route_id) is not UUID:
        raise _invalid()
    if type(cancel) is not CancelToken:
        raise _invalid()
    resources = ledger._resources
    database = resources._database
    scope = _require_scope(database)
    runtime = scope.runtime
    progress = _RecoveryProgress()
    budget, control, caller_checkpoint = _recovery_control(ledger, cancel, checkpoint, progress)

    timeout = asyncio.timeout(budget.remaining())
    try:
        async with timeout:
            control()
            route = resources._router.status(route_id)
            control()
            if type(route) is not ActionRouteSnapshotV2:
                raise _invalid()
            core = load_product_git_delivery_route_core_v2(
                resources._core_store, route.plan, checkpoint=control
            )
            require_prepared_git_runtime_thread(database, core.thread_id)
            thread = await runtime.store.get_thread(core.thread_id)
            control()
            turn = get_turn(thread, core.turn_id)
            progress.turn = turn
            control()
            _limit_to_original_turn(core, turn, budget, timeout)
            _require_original_prepared_target(resources, runtime, route, core, control)
            control()
            actions = runtime._trusted_actions
            if actions is None:
                raise _invalid()
            await actions._sync_decision_in_owned_thread(core.thread_id, core.turn_id)
            control()
            if _require_scope(database) is not scope:
                raise _invalid()
            # 从这里开始仅清理本屏障开启的事务，绝不回滚调用方已有事务。
            try:
                database.execute("BEGIN IMMEDIATE")
                progress.epoch = _git_prefix_caller_transaction_epoch(database)
                progress.observe_transaction = _git_prefix_caller_transaction_observer(database)
                result = await ledger._append_decision(
                    route_id, cancel=cancel, checkpoint=caller_checkpoint, budget=budget
                )
                control()
                if _require_scope(database) is not scope:
                    raise _invalid()
                _require_git_prefix_caller_transaction_epoch(database, progress.epoch)
                # 此后无 await / 外部 callback；原提交门继续使用同一绝对预算。
                with prepared_git_commit_scope(database):
                    database.execute("COMMIT")
                return result
            except BaseException:
                try:
                    if progress.epoch is not None:
                        # 只接管本屏障原代际；回调撤销并新开事务后，禁止代管替换事务。
                        _require_git_prefix_caller_transaction_epoch(database, progress.epoch)
                        database.execute("ROLLBACK")
                except BaseException:
                    # 清理失败不遮蔽原首失败；原连接仍由调用方关闭，不复用为成功。
                    pass
                raise
    except TimeoutError:
        if timeout.expired():
            raise KernelError("git_process_timeout", "Git决定恢复总期限已耗尽") from None
        raise
