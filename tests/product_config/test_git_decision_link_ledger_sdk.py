"""原认证 SDK 决定经正式 Ledger 提交、精确复用与只读重开；不执行 Git 写效果。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemFinished,
    ItemStatus,
    ToolResultContent,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.domain.models import ApprovalOutcome
from harnessix.product_config.git_decision_link_contracts import (
    ProductGitApprovedLink,
    ProductGitCancelledLink,
    ProductGitDeniedLink,
)
from harnessix.product_config.git_decision_link_ledger import ProductGitDecisionLinkLedger
from harnessix.product_config.git_prefix_sql import _dispatch_git_prefix_trace
from harnessix.product_config.git_prepared_runtime_thread import prepared_git_commit_scope
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_prepared_approval_history import _decide, _reader
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _history,
    _ledger,
    _rows,
)
from tests.support.git_decision_recovery import prepare_original_link


def _original_effects(actual):
    """冻结决定完成后的原状态及写计数，区分业务决定与 Ledger 后续效果。"""
    scenario = actual.scenario
    route_id = actual.route.plan.execution.plan_id
    return (
        scenario.unchanged_state(),
        scenario.router._audit._db.total_changes,
        scenario.router._plans._db.total_changes,
        scenario.router.events(route_id),
        scenario.router.approval(route_id),
        _source_snapshot(scenario.root),
        _source_snapshot(actual.preparer.worktree_parent),
    )


def _observe_dml(database):
    """记录实际 DML 语句，同时转发原事务 trace，保留原提交代际检查。"""
    statements = []

    def observe(statement):
        _dispatch_git_prefix_trace(database, statement)
        if statement.lstrip().split(maxsplit=1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
        }:
            statements.append(statement)

    database.set_trace_callback(observe)
    return statements


def _assert_settled_cancel(fact, history):
    """核对审批前取消的四个原事件及无效果结果，不把系统拒绝冒充人工决定。"""
    events = {event.event_id: event for event in history.events}
    cancelling = events[fact.cancel_event.event_id].payload
    approval = events[fact.approval_cancel_event.event_id].payload
    result = events[fact.call_result_event.event_id].payload
    terminal = events[fact.turn_terminal_event.event_id].payload
    assert type(cancelling) is TurnStateChanged and cancelling.status is TurnStatus.CANCELLING
    assert type(approval) is ItemFinished and approval.status is ItemStatus.CANCELLED
    assert approval.content == fact.approval_request and approval.content.decision is None
    assert type(result) is ItemFinished and result.status is ItemStatus.COMPLETED
    assert type(result.content) is ToolResultContent and result.content.outcome == "cancelled"
    assert all(
        value is None
        for value in (
            result.content.output,
            result.content.action_id,
            result.content.patch,
            result.content.patch_batch,
            result.content.process,
            result.content.trusted_action,
            result.content.diff_artifact,
        )
    )
    assert result.content.error is not None and result.content.error.code == "cancelled"
    assert type(terminal) is TurnStateChanged and terminal.status is TurnStatus.CANCELLED
    decision = fact.router_approval.decision
    assert decision.outcome is ApprovalOutcome.REJECTED
    assert (decision.actor, decision.reason) == ("system.cancel", "turn_cancelled")
    assert not hasattr(fact, "session_decision") and not hasattr(fact, "decision_event")


def _assert_human_decision(fact, history):
    """原 Session 决定正文与原 Execution 检查点一致，但各自保留原指纹域。"""
    event = next(
        event for event in history.events if event.event_id == fact.decision_event.event_id
    )
    assert type(event.payload) is ItemFinished and event.payload.status is ItemStatus.COMPLETED
    assert event.payload.content == fact.session_decision
    session_decision = fact.session_decision.decision
    router_decision = fact.router_approval.decision
    assert session_decision is not None
    assert session_decision.request_fingerprint == fact.approval_request.request_fingerprint
    assert router_decision.request_fingerprint == fact.plan.route.execution.fingerprint
    assert (
        session_decision.outcome,
        session_decision.actor,
        session_decision.reason,
        session_decision.decided_at,
    ) == (
        router_decision.outcome,
        router_decision.actor,
        router_decision.reason,
        router_decision.decided_at,
    )
    assert session_decision.outcome is (
        ApprovalOutcome.APPROVED
        if type(fact) is ProductGitApprovedLink
        else ApprovalOutcome.REJECTED
    )


def _assert_original_events(fact, history):
    """决定引用须来自同一原 Turn 的认证历史，按原事件与正文定位严格有序。"""
    events = {event.event_id: event for event in history.events}
    bodies = {ref.event_id: ref for ref in history.body_refs}
    references = (
        (
            fact.request_event,
            fact.cancel_event,
            fact.approval_cancel_event,
            fact.call_result_event,
            fact.turn_terminal_event,
        )
        if type(fact) is ProductGitCancelledLink
        else (fact.request_event, fact.decision_event)
    )
    assert all(
        before.sequence < after.sequence
        for before, after in zip(references, references[1:], strict=False)
    )
    for ref in references:
        event, body = events[ref.event_id], bodies[ref.event_id]
        assert event.thread_id == fact.plan.core.thread_id
        assert event.turn_id == fact.plan.core.turn_id
        assert ref.sequence == event.sequence == body.sequence
        assert ref.digest == body.body_sha256


@pytest.mark.parametrize(
    ("kind", "link_type", "phase"),
    [
        ("approved", ProductGitApprovedLink, "approved"),
        ("denied", ProductGitDeniedLink, "failed"),
        ("cancelled", ProductGitCancelledLink, "failed"),
    ],
)
async def test_actual_sdk_append_decision_commit_exact_retry_and_read_only_reopen(
    tmp_path, config, monkeypatch, kind, link_type, phase
):
    """真实审批或完整取消只追加唯一事实，重试零 DML，重开不改变原业务来源。"""

    async def inspect(actual):
        scenario = actual.scenario
        prepared, baseline = await prepare_original_link(actual)
        route_id = actual.route.plan.execution.plan_id
        assert prepared.plan.core.thread_id == actual.thread.thread_id
        assert prepared.plan.core.turn_id == actual.turn.turn_id
        assert prepared.plan.core.call == actual.call
        assert prepared.plan.route == actual.route.plan
        assert prepared.approval.decision is None
        if kind == "cancelled":
            cancelled = await scenario.client.cancel_turn(
                actual.thread.thread_id, actual.turn.turn_id, request_id="decision-ledger-cancel"
            )
            assert cancelled.status == "cancelled"
        else:
            spawns = await _decide(
                actual, monkeypatch, "approved" if kind == "approved" else "rejected"
            )
            assert spawns == [(actual.thread.thread_id, actual.turn.turn_id)]
        history, effects = await _history(actual), _original_effects(actual)
        async with _database(actual) as database:
            assert _rows(database) == baseline
            ledger = ProductGitDecisionLinkLedger(_ledger(actual, database))
            database.execute("BEGIN IMMEDIATE")
            changes = database.total_changes
            fact = await ledger.append_decision(
                route_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert database.in_transaction and database.total_changes > changes
            assert type(fact) is link_type and (fact.fact_kind, fact.phase) == (kind, phase)
            assert fact.sequence == 1 and fact.plan == prepared.plan
            assert fact.approval_request == prepared.approval
            assert fact.router_approval == scenario.router.approval(route_id)
            _assert_original_events(fact, history)
            if kind == "cancelled":
                _assert_settled_cancel(fact, history)
            else:
                _assert_human_decision(fact, history)
            with prepared_git_commit_scope(database):
                database.execute("COMMIT")
            assert not database.in_transaction
            committed = _rows(database)
            assert committed != baseline
            assert database.execute(
                "SELECT phase,sequence FROM git_product_links WHERE route_id=?", (str(route_id),)
            ).fetchone() == (phase, 1)
            assert database.execute(
                "SELECT sequence,phase FROM git_product_link_events WHERE route_id=? "
                "ORDER BY sequence",
                (str(route_id),),
            ).fetchall() == [(0, "prepared"), (1, phase)]
            assert await _history(actual) == history and _original_effects(actual) == effects
            dml, changes = _observe_dml(database), database.total_changes
            database.execute("BEGIN IMMEDIATE")
            duplicate = await ledger.append_decision(
                route_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert duplicate == fact and duplicate is not fact and database.in_transaction
            with prepared_git_commit_scope(database):
                database.execute("COMMIT")
            assert not database.in_transaction and not dml
            assert database.total_changes == changes and _rows(database) == committed
            assert await _history(actual) == history and _original_effects(actual) == effects
        async with _database(actual, read_only=True) as database:
            assert database.execute("PRAGMA query_only").fetchone() == (1,)
            database.execute("BEGIN")
            reopened = await _reader(actual, database).read_linked_decision(
                route_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert reopened == fact and reopened is not fact
            assert database.in_transaction and database.total_changes == 0
            assert _rows(database) == committed
            database.execute("ROLLBACK")
        assert await _history(actual) == history and _original_effects(actual) == effects
        assert not list(actual.preparer.worktree_parent.iterdir())

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_actual_sdk_cancel_after_approval_cannot_append_preapproval_cancelled(
    tmp_path, config, monkeypatch
):
    """原 SDK 批准后取消保留人工批准；拒绝追加，不将未知结算伪装为审批前取消。"""

    async def inspect(actual):
        scenario = actual.scenario
        prepared, baseline = await prepare_original_link(actual)
        route_id = prepared.plan.route.execution.plan_id
        await _decide(actual, monkeypatch, "approved")
        approval = scenario.router.approval(route_id)
        assert approval is not None and approval.decision.outcome is ApprovalOutcome.APPROVED
        cancelled = await scenario.client.cancel_turn(
            actual.thread.thread_id,
            actual.turn.turn_id,
            request_id="decision-ledger-cancel-approved",
        )
        # 原 ready 调用未执行、没有确定结果，真实取消链以 interrupted 而非 cancelled 结算。
        assert cancelled.status == "interrupted"
        assert scenario.router.status(route_id).state == "ready"
        assert scenario.router.approval(route_id) == approval
        history, effects = await _history(actual), _original_effects(actual)
        assert any(
            event.turn_id == actual.turn.turn_id
            and type(event.payload) is TurnStateChanged
            and event.payload.status is TurnStatus.CANCELLING
            for event in history.events
        )
        async with _database(actual) as database:
            assert _rows(database) == baseline
            dml, changes = _observe_dml(database), database.total_changes
            database.execute("BEGIN IMMEDIATE")
            with pytest.raises(KernelError) as caught:
                await ProductGitDecisionLinkLedger(_ledger(actual, database)).append_decision(
                    route_id, cancel=CancelToken(), checkpoint=lambda: None
                )
            assert caught.value.code == "git_approval_history_changed"
            assert database.in_transaction and database.total_changes == changes and not dml
            assert _rows(database) == baseline
            database.execute("ROLLBACK")
            assert _rows(database) == baseline
        async with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await _reader(actual, database).read_linked_decision(
                    route_id, cancel=CancelToken(), checkpoint=lambda: None
                )
            assert caught.value.code == "git_approval_history_changed"
            assert database.total_changes == 0 and _rows(database) == baseline
            database.execute("ROLLBACK")
        assert await _history(actual) == history and _original_effects(actual) == effects
        assert not list(actual.preparer.worktree_parent.iterdir())

    await _case(tmp_path, config, monkeypatch, inspect)
