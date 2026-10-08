"""内部原事务决定追加；不装配默认产品，不签发 Git 效果授权。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_baseline import _BASELINE_TIMEOUT_SECONDS
from harnessix.product_config.git_decision_link_contracts import ProductGitDecisionLink
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from harnessix.product_config.git_decision_link_wire import encode_product_git_decision_link
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    publish_git_prefix_changes,
)
from harnessix.product_config.git_prepared_approval_history import _DecidedReadSet, _read_linked_all
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_ledger import ProductGitPreparedLinkLedger, _control
from harnessix.product_config.git_prepared_link_rows import prepared_link_columns
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.product_config.git_prepared_runtime_thread import require_prepared_git_runtime_thread
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims


def _invalid(code: str) -> KernelError:
    return KernelError(code, "Git原决定关联不能追加或复用")


class ProductGitDecisionLinkLedger:
    """只借同一原 Ledger，消费原完整来源；不接受调用方提交决定或证明。"""

    def __init__(self, prepared: ProductGitPreparedLinkLedger) -> None:
        if type(prepared) is not ProductGitPreparedLinkLedger:
            raise _invalid("git_decision_link_resources_invalid")
        self._resources = prepared

    async def append_decision(
        self, route_id: UUID, *, cancel: CancelToken, checkpoint: Callable[[], None]
    ) -> ProductGitDecisionLink:
        """返回已终端复验但未提交的事实；事务提交或失败回滚始终由原调用方处理。"""
        await asyncio.sleep(0)
        return await self._append_decision(
            route_id,
            cancel=cancel,
            checkpoint=checkpoint,
            budget=GitOperationBudget(_BASELINE_TIMEOUT_SECONDS),
        )

    async def _append_decision(
        self,
        route_id: UUID,
        *,
        cancel: CancelToken,
        checkpoint: Callable[[], None],
        budget: GitOperationBudget,
    ) -> ProductGitDecisionLink:
        """恢复宿主借同一绝对期限；唯一算法不重新分配操作预算。"""
        return await _read_and_append_decision(
            self, route_id, cancel=cancel, checkpoint=checkpoint, budget=budget
        )

    async def _append(
        self,
        prepared: ProductGitPreparedLink,
        result: ProductGitDecisionLink,
        cancel: CancelToken,
        check: Callable[[], None],
    ) -> None:
        """精确消费既有原写窗口，在同一事务发布原行、事件和 MAC。"""
        await _append_product_rows(self._resources, prepared, result, cancel, check)


async def _read_and_append_decision(
    ledger: ProductGitDecisionLinkLedger,
    route_id: UUID,
    *,
    cancel: CancelToken,
    checkpoint: Callable[[], None],
    budget: GitOperationBudget,
) -> ProductGitDecisionLink:
    """全来源读取、唯一决定追加和终端结果核对，不负责调用方的 COMMIT。"""
    if type(route_id) is not UUID or type(budget) is not GitOperationBudget:
        raise _invalid("git_decision_source_invalid")
    resources = ledger._resources
    await asyncio.sleep(0)
    read_set = _DecidedReadSet()
    with _control(resources, cancel, budget, checkpoint, read_set, retain_for_commit=True) as check:
        timeout = asyncio.timeout(budget.remaining())
        try:
            async with timeout:
                if resources._database.execute("PRAGMA query_only").fetchone() == (1,):
                    raise KernelError("git_delivery_store_read_only", "Git交付只读账本不接受写入")
                histories = await _read_linked_all(resources, cancel, budget, check, read_set)
                history = next(
                    (
                        item
                        for item in histories
                        if item.prepared.plan.route.execution.plan_id == route_id
                    ),
                    None,
                )
                if history is None:
                    raise _invalid("git_decision_source_missing")
                evidence = read_set.approvals[route_id]
                if evidence.projection.state == "pending":
                    raise _invalid("git_decision_source_pending")
                require_prepared_git_runtime_thread(
                    resources._database, history.prepared.plan.core.thread_id
                )
                result = build_git_decision_link_sources(evidence, checkpoint=check)
                if history.decision is None:
                    await ledger._append(history.prepared, result, cancel, check)
                    histories = await _read_linked_all(resources, cancel, budget, check, read_set)
                elif history.decision != result:
                    raise _invalid("git_decision_link_conflict")
                read_set.linked = histories
                read_set.declaration = (route_id, result)
                check()
        except TimeoutError:
            if timeout.expired():
                raise _invalid("git_process_timeout") from None
            raise
    if read_set.validated_result is None:
        raise _invalid("git_decision_link_history_changed")
    return read_set.validated_result


async def _append_product_rows(
    resources: ProductGitPreparedLinkLedger,
    prepared: ProductGitPreparedLink,
    result: ProductGitDecisionLink,
    cancel: CancelToken,
    check: Callable[[], None],
) -> None:
    """原受控事务内的唯一 DML 配方；精确复用路径绝不进入本函数。"""
    route_id = result.plan.route.execution.plan_id
    database, publication = resources._database, resources._artifacts.session._publication
    assert publication is not None
    body = encode_product_git_decision_link(result, checkpoint=check).decode("utf-8")
    previous = prepared_link_columns(
        prepared, encode_product_git_prepared_link(prepared, checkpoint=check)
    )
    window = begin_git_prefix_write(
        database, publication.git, publication.git_prefix, checkpoint=check
    )
    stream = next(item for item in window.catalog.streams if item.first.record_id == route_id)
    check()
    cursor = database.execute(
        "UPDATE git_product_links SET phase=?,sequence=1,payload=? WHERE "
        "route_id=? AND delivery_id=? AND thread_id=? AND turn_id=? AND call_id=? AND "
        "action_kind=? AND core_sha256=? AND route_fingerprint=? AND phase=? AND "
        "sequence=? AND payload=?",
        (result.phase, body, *previous),
    )
    if cursor.rowcount != 1:
        raise _invalid("git_decision_link_conflict")
    check()
    database.execute(
        "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
        (str(route_id), 1, result.phase, body),
    )
    check()
    first = stream.first
    claims = GitDeliveryRecordClaims(
        record_kind=first.record_kind,
        record_id=first.record_id,
        route_id=first.route_id,
        delivery_id=first.delivery_id,
        thread_id=first.thread_id,
        turn_id=first.turn_id,
        call_id=first.call_id,
        sequence=2,
        previous_sha256=stream.prefix_sha256,
        publication_epoch=first.publication_epoch,
    )
    await publish_git_prefix_changes(
        window,
        (claims,),
        publication.git,
        publication.git_prefix,
        publication._events._protection,
        cancel=cancel,
    )
    check()
