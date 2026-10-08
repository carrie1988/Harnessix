"""复用原认证 SDK 夹具，停在 Router 决定已持久化、Session 尚未同步的窗口。"""

from __future__ import annotations

from harnessix.agent.cancellation import CancelToken
from harnessix.domain.models import ApprovalDecision
from harnessix.product_config import git_decision_recovery as recovery
from harnessix.product_config.git_decision_link_ledger import ProductGitDecisionLinkLedger
from harnessix.product_config.git_prepared_runtime_thread import prepared_git_commit_scope
from tests.product_config.test_git_prepared_link_ledger import (
    _database,
    _genesis,
    _ledger,
    _rows,
)


async def prepare_original_link(actual):
    """沿原工厂与持锁 Task 准备关联，通过原提交门返回 (prepared, baseline_rows)。"""
    async with _database(actual) as database:
        await _genesis(actual, database)
        database.execute("BEGIN IMMEDIATE")
        prepared = await _ledger(actual, database).prepare(
            actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
        )
        with prepared_git_commit_scope(database):
            database.execute("COMMIT")
        baseline_rows = _rows(database)
    return prepared, baseline_rows


async def router_first(actual, outcome):
    """在真实 Router 已持久化、Session 尚未同步处停止，不伪造决定来源。"""
    runtime = actual.scenario.client.transport.server.service.runtime
    actions = runtime._trusted_actions
    assert actions is not None
    prepared, baseline = await prepare_original_link(actual)
    actions._state.gateway.decide(
        actual.thread,
        actual.turn,
        actual.call,
        prepared.approval,
        ApprovalDecision(outcome=outcome, actor="original-owned-sync"),
    )
    return runtime, actions, baseline


async def recover_original_decision(actual, database, *, token=None, check=lambda: None):
    """仅转发原账本、原计划及取消参数，恢复逻辑始终来自生产实现。"""
    return await recovery.recover_decision_link(
        ProductGitDecisionLinkLedger(_ledger(actual, database)),
        actual.route.plan.execution.plan_id,
        cancel=token or CancelToken(),
        checkpoint=check,
    )
