"""原 SDK prepared 与审批历史消费完整 U；不签发决定或开放产品 Git 写工具。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from harnessix.agent.approvals import remaining_seconds
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, TrustedActionApprovalRequestContent
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.product_config import git_prepared_approval_history as history_module
from harnessix.product_config import git_prepared_link_ledger as ledger_module
from harnessix.product_config import git_user_observation as observation_module
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    publish_git_prefix_changes,
)
from harnessix.product_config.git_prepared_link_proof import authenticate_prepared_link
from harnessix.product_config.git_prepared_link_rows import prepared_link_columns
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.protocol.contracts import ApprovalRespondParams, PublicApprovalDecision
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from tests.agent.helpers import answer
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_prepared_approval_history import _decide, _prepared, _reader
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _history,
    _ledger,
    _rows,
)
from tests.product_config.test_git_user_observation_verification import _readonly_state
from tests.product_config.test_product_rollback_sdk import wait_turn


def _record_verification(monkeypatch, actual):
    """只观测调用，始终委托原 verifier；不以模拟成功替代真实 Git/Session 认证。"""
    calls = []

    async def verified(expected, history, router, transactions, reader, **controls):
        scenario = actual.scenario
        assert router is scenario.router and transactions is scenario.transactions
        assert reader is scenario.reader and controls["session"] is scenario.session
        assert controls["snapshot_ports"] is scenario.router._snapshot_ports
        assert expected.baseline.source.thread_id == history.thread.thread_id
        budget = controls["budget"]
        deadline = budget._deadline
        calls.append((budget, deadline, controls["cancel"], controls["checkpoint"]))
        await observation_module.verify_product_git_user_observation(
            expected, history, router, transactions, reader, **controls
        )
        assert budget._deadline == deadline

    for module in (ledger_module, history_module):
        monkeypatch.setattr(module, "verify_product_git_user_observation", verified, raising=False)
    return calls


def _same_controls(calls, token):
    """同一次消费者操作复用唯一预算、原取消令牌及同一原协调检查点。"""
    assert calls, "真实消费者尚未调用完整 U verifier"
    budget, deadline, cancel, checkpoint = calls[0]
    assert cancel is token
    assert all(
        item[0] is budget and item[1] == deadline and item[2] is token and item[3] is checkpoint
        for item in calls
    )


async def _consume(actual, database, operation, *, cancel, checkpoint):
    """选择原公开消费者，不另建 Store、来源或恢复入口。"""
    if operation == "prepare":
        return await _ledger(actual, database).prepare(
            actual.route.plan.execution.plan_id, cancel=cancel, checkpoint=checkpoint
        )
    if operation == "pending-read":
        return await _ledger(actual, database).read_all(cancel=cancel, checkpoint=checkpoint)
    return await _reader(actual, database).read_all(cancel=cancel, checkpoint=checkpoint)


@pytest.mark.parametrize(
    "fmt,state",
    [("sha1", "pending"), ("sha256", "approved"), ("sha1", "denied"), ("sha256", "cancelled")],
)
async def test_real_prepare_and_history_consume_u_without_recapture_or_new_writes(
    tmp_path, config, monkeypatch, fmt, state
):
    async def inspect(actual):
        scenario = actual.scenario
        calls = _record_verification(monkeypatch, actual)
        before = _readonly_state(scenario)

        def forbidden(*_args, **_kwargs):
            pytest.fail("消费既有 U 不得重新收集、写 CAS 或启动 Git 执行/对账")

        with monkeypatch.context() as patch:
            patch.setattr(observation_module, "collect_git_delivery_source", forbidden)
            patch.setattr(observation_module, "_collect_baseline_from_source", forbidden)
            patch.setattr(SQLiteWorkspaceTransactionStore, "_put_blob", forbidden)
            patch.setattr(scenario.router, "execute", forbidden)
            patch.setattr(scenario.router, "reconcile", forbidden)
            with _database(actual) as database:
                await _genesis(actual, database)
                database.execute("BEGIN IMMEDIATE")
                token = CancelToken()
                link = await _consume(
                    actual, database, "prepare", cancel=token, checkpoint=lambda: None
                )
                _same_controls(calls, token)
                assert len(calls) == 2
                database.execute("COMMIT")
                sealed = _rows(database)
            assert _readonly_state(scenario) == before
        if state in {"approved", "denied"}:
            await _decide(actual, monkeypatch, "approved" if state == "approved" else "rejected")
        elif state == "cancelled":
            await scenario.client.cancel_turn(
                actual.thread.thread_id, actual.turn.turn_id, request_id="consumption-cancel"
            )
        before = _readonly_state(scenario)
        history = await _history(actual)
        calls.clear()
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            result = await _consume(
                actual, database, "history-read", cancel=token, checkpoint=lambda: None
            )
            _same_controls(calls, token)
            assert result[0].prepared == link
            assert result[0].approval_history.state == state
            assert result[0].linkage_state == (
                "prepared" if state == "pending" else "decision_not_linked"
            )
            assert not hasattr(result[0], "can_execute")
            assert database.total_changes == 0 and _rows(database) == sealed
            if state != "pending":
                with pytest.raises(KernelError):
                    await _ledger(actual, database).read_all(cancel=token, checkpoint=lambda: None)
        assert await _history(actual) == history
        assert _readonly_state(scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect, fmt=fmt, continuous=state == "denied")


@pytest.mark.parametrize("operation", ["prepare", "pending-read", "history-read"])
@pytest.mark.parametrize("fault", ["config-value", "head-ref", "physical-index"])
async def test_frozen_u_drift_rejects_real_consumers_and_prepare_rolls_back(
    tmp_path, config, monkeypatch, operation, fault
):
    async def inspect(actual):
        scenario = actual.scenario
        if operation != "prepare":
            await _prepared(actual)
        else:
            with _database(actual) as database:
                await _genesis(actual, database)
        if fault == "config-value":
            names = command(scenario.root, "config", "--no-includes", "--name-only", "--list")
            command(scenario.root, "config", "user.name", "External Changed Name")
            assert (
                command(scenario.root, "config", "--no-includes", "--name-only", "--list") == names
            )
        elif fault == "head-ref":
            head = command(scenario.root, "rev-parse", "HEAD").strip().decode()
            command(scenario.root, "update-ref", "refs/heads/external", head)
            command(scenario.root, "symbolic-ref", "HEAD", "refs/heads/external")
        else:
            index = scenario.root / ".git/index"
            replacement = index.with_name("index.replacement")
            replacement.write_bytes(index.read_bytes())
            os.replace(replacement, index)
        before = _readonly_state(scenario)
        with _database(actual, read_only=operation != "prepare") as database:
            rows = _rows(database)
            database.execute("BEGIN IMMEDIATE" if operation == "prepare" else "BEGIN")
            with pytest.raises(KernelError) as caught:
                await _consume(
                    actual, database, operation, cancel=CancelToken(), checkpoint=lambda: None
                )
            assert caught.value.code == "git_user_observation_changed"
            database.execute("ROLLBACK")
            assert _rows(database) == rows
            if operation != "prepare":
                assert database.total_changes == 0
        assert _readonly_state(scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect)


async def _next_pending_after_rejection(actual):
    """沿同一真实 SDK/Gateway 终结第一请求，再规划第二请求；不伪造历史或 Core。"""
    scenario = actual.scenario
    request = next(
        item.content
        for item in actual.turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    )
    scenario.bundle.steps = (*scenario.bundle.steps, answer("请求已拒绝"))
    await scenario.client.respond_approval(
        ApprovalRespondParams(
            request_id="non-target-reject",
            thread_id=actual.thread.thread_id,
            turn_id=actual.turn.turn_id,
            approval_id=request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=PublicApprovalDecision(outcome="rejected", actor="original-reviewer"),
        )
    )
    await wait_turn(scenario.client, actual.thread.thread_id, "completed")
    command(scenario.root, "config", "user.name", "Next Original Request")
    scenario.bundle.steps = (
        [
            ResponseStarted(response_id="second-consumption-response"),
            ToolCallCompleted(
                call_id="second-consumption-call",
                tool="git_checkpoint",
                arguments=actual.call.arguments,
            ),
            ResponseCompleted(finish_reason="tool_calls"),
        ],
    )
    await scenario.client.start_turn(
        actual.thread.thread_id, "审阅本地交付", request_id="second-consumption-pending"
    )
    await wait_turn(scenario.client, actual.thread.thread_id, "waiting_approval")
    thread = await scenario.session.get_thread(actual.thread.thread_id)
    turn = thread.turns[-1]
    call = next(item.content for item in turn.items if type(item.content) is ToolCallContent)
    approval = next(
        item.content
        for item in turn.items
        if type(item.content) is TrustedActionApprovalRequestContent
    )
    return replace(
        actual, thread=thread, turn=turn, call=call, route=scenario.router.status(approval.plan_id)
    )


async def _publish_authenticated_second_predecessor(actual, database):
    """全集夹具：原 pending Proof 产生真实第二前驱，原物理发布器签发；不是产品 Writer。"""
    token, budget = CancelToken(), GitOperationBudget(60.0)
    evidence = await authenticate_prepared_link(
        actual.route.plan.execution.plan_id,
        actual.scenario.router,
        actual.preparer.core_store,
        actual.artifacts,
        actual.scenario.router._snapshot_ports,
        actual.provider._workspace_scope,
        cancel=token,
        budget=budget,
        checkpoint=token.checkpoint,
    )
    link = evidence.link
    await observation_module.verify_product_git_user_observation(
        link.plan.core.user_observation,
        evidence.history,
        actual.scenario.router,
        actual.scenario.transactions,
        actual.scenario.reader,
        session=actual.scenario.session,
        cancel=token,
        budget=budget,
        checkpoint=token.checkpoint,
        snapshot_ports=actual.scenario.router._snapshot_ports,
    )
    core, publication = link.plan.core, actual.scenario.session._publication
    body = encode_product_git_prepared_link(link, checkpoint=token.checkpoint)
    with git_prefix_sql_window(database, checkpoint=token.checkpoint):
        window = begin_git_prefix_write(
            database, publication.git, publication.git_prefix, checkpoint=token.checkpoint
        )
        columns = prepared_link_columns(link, body)
        database.execute("INSERT INTO git_product_links VALUES (?,?,?,?,?,?,?,?,?,?,?)", columns)
        database.execute(
            "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
            (str(actual.route.plan.execution.plan_id), 0, "prepared", columns[-1]),
        )
        claims = GitDeliveryRecordClaims(
            record_kind="product_link",
            record_id=actual.route.plan.execution.plan_id,
            publication_epoch=uuid4(),
            sequence=1,
            previous_sha256="0" * 64,
            delivery_id=core.delivery_id,
            thread_id=core.thread_id,
            turn_id=core.turn_id,
            call_id=core.call.call_id,
            route_id=actual.route.plan.execution.plan_id,
        )
        await publish_git_prefix_changes(
            window,
            (claims,),
            publication.git,
            publication.git_prefix,
            publication._events._protection,
            cancel=token,
        )
    return link


async def test_valid_target_does_not_hide_non_target_original_u_drift(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        first, _ = await _prepared(actual)
        second = await _next_pending_after_rejection(actual)
        with _database(actual) as database:
            database.execute("BEGIN IMMEDIATE")
            target = await _publish_authenticated_second_predecessor(second, database)
            database.execute("COMMIT")
            assert first.plan.route.execution.plan_id != target.plan.route.execution.plan_id
            assert (
                first.plan.core.user_observation.config_sha256
                != target.plan.core.user_observation.config_sha256
            )
            assert database.execute("SELECT COUNT(*) FROM git_product_links").fetchone() == (2,)
        before = _readonly_state(actual.scenario)
        with _database(actual, read_only=True) as database:
            rows = _rows(database)
            database.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await _reader(second, database).read_all(
                    cancel=CancelToken(), checkpoint=lambda: None
                )
            assert caught.value.code == "git_user_observation_changed"
            assert database.total_changes == 0 and _rows(database) == rows
        assert _readonly_state(actual.scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_router_first_recovery_uses_original_sync_then_reads_u_without_new_authorization(
    tmp_path, config, monkeypatch
):
    from harnessix.domain.models import ApprovalDecision, ApprovalOutcome

    async def inspect(actual):
        link, rows = await _prepared(actual)
        calls = _record_verification(monkeypatch, actual)
        actions = actual.scenario.client.transport.server.service.runtime._trusted_actions
        actions._state.gateway.decide(
            actual.thread,
            actual.turn,
            actual.call,
            link.approval,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="original-recovery"),
        )
        before = await _history(actual)
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await _reader(actual, database).read_all(
                    cancel=CancelToken(), checkpoint=lambda: None
                )
            assert caught.value.code == "git_approval_history_changed"
            assert not calls and database.total_changes == 0
        assert await _history(actual) == before
        await actions.sync_decision(actual.thread.thread_id, actual.turn.turn_id)
        after = await _history(actual)
        assert after != before
        stable = _readonly_state(actual.scenario)
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            token = CancelToken()
            result = await _reader(actual, database).read_all(cancel=token, checkpoint=lambda: None)
            _same_controls(calls, token)
            assert result[0].approval_history.state == "approved"
            assert result[0].linkage_state == "decision_not_linked"
            assert database.total_changes == 0 and _rows(database) == rows
        assert await _history(actual) == after
        assert _readonly_state(actual.scenario) == stable

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("operation", ["prepare", "pending-read", "history-read"])
@pytest.mark.parametrize(
    "error_kind", ["kernel", "os", "sqlite", "turn-cancel", "timeout", "task-cancel"]
)
async def test_original_checkpoint_exceptions_inside_real_u_verifier_are_preserved(
    tmp_path, config, monkeypatch, operation, error_kind
):
    async def inspect(actual):
        if operation != "prepare":
            await _prepared(actual)
        with _database(actual, read_only=operation != "prepare") as database:
            if operation == "prepare":
                await _genesis(actual, database)
            rows = _rows(database)
            before = _readonly_state(actual.scenario)
            active = False

            async def verified(*args, **kwargs):
                nonlocal active
                active = True
                try:
                    return await observation_module.verify_product_git_user_observation(
                        *args, **kwargs
                    )
                finally:
                    active = False

            module = history_module if operation == "history-read" else ledger_module
            monkeypatch.setattr(
                module, "verify_product_git_user_observation", verified, raising=False
            )
            marker = {
                "kernel": KernelError("consumption_checkpoint", "原检查点异常"),
                "os": OSError("original checkpoint"),
                "sqlite": sqlite3.OperationalError("original checkpoint"),
                "turn-cancel": TurnCancelled("original checkpoint"),
                "timeout": TimeoutError("original checkpoint"),
                "task-cancel": asyncio.CancelledError("original checkpoint"),
            }[error_kind]

            def checkpoint():
                if active:
                    raise marker

            database.execute("BEGIN IMMEDIATE" if operation == "prepare" else "BEGIN")
            with pytest.raises(type(marker)) as caught:
                await _consume(
                    actual, database, operation, cancel=CancelToken(), checkpoint=checkpoint
                )
            assert caught.value is marker
            database.execute("ROLLBACK")
            assert _rows(database) == rows
            assert _readonly_state(actual.scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_post_publication_u_control_failure_rolls_back_event_mac_and_tail(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        calls, active = 0, False
        marker = OSError("post-publication original checkpoint")

        async def verified(*args, **kwargs):
            nonlocal calls, active
            calls += 1
            active = calls == 2
            try:
                return await observation_module.verify_product_git_user_observation(*args, **kwargs)
            finally:
                active = False

        monkeypatch.setattr(
            ledger_module, "verify_product_git_user_observation", verified, raising=False
        )
        with _database(actual) as database:
            await _genesis(actual, database)
            rows = _rows(database)
            before = _readonly_state(actual.scenario)

            def checkpoint():
                if active:
                    assert database.execute(
                        "SELECT COUNT(*) FROM git_product_link_events"
                    ).fetchone() == (1,)
                    raise marker

            database.execute("BEGIN IMMEDIATE")
            with pytest.raises(OSError) as caught:
                await _consume(
                    actual, database, "prepare", cancel=CancelToken(), checkpoint=checkpoint
                )
            assert caught.value is marker and calls == 2
            database.execute("ROLLBACK")
            assert _rows(database) == rows
            assert _readonly_state(actual.scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_real_prepared_commit_reopen_and_exact_retry_share_controls_without_new_writes(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        link, sealed = await _prepared(actual)
        calls = _record_verification(monkeypatch, actual)
        before = _readonly_state(actual.scenario)
        with _database(actual) as database:
            database.execute("BEGIN IMMEDIATE")
            token = CancelToken()
            assert (
                await _consume(actual, database, "prepare", cancel=token, checkpoint=lambda: None)
                == link
            )
            _same_controls(calls, token)
            assert len(calls) == 2
            database.execute("COMMIT")
            assert _rows(database) == sealed
        assert _readonly_state(actual.scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect, fmt="sha256")


async def test_original_turn_real_expiry_refuses_approval_and_consumers_without_writes(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        _, sealed = await _prepared(actual)
        original = (actual.turn.created_at, actual.turn.budget)
        started = time.monotonic()
        remaining = remaining_seconds(actual.turn)
        assert remaining > 0
        await asyncio.sleep(remaining + 0.05)
        assert time.monotonic() - started >= remaining
        assert remaining_seconds(actual.turn) < 0
        before = _readonly_state(actual.scenario)
        with pytest.raises(KernelError) as expired:
            await _decide(actual, monkeypatch, "approved")
        assert expired.value.code == "approval_expired"
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            with pytest.raises(KernelError) as history:
                await _reader(actual, database).read_all(
                    cancel=CancelToken(), checkpoint=lambda: None
                )
            assert history.value.code == "git_approval_history_changed"
            assert database.total_changes == 0 and _rows(database) == sealed
        current = await actual.scenario.session.get_thread(actual.thread.thread_id)
        assert (current.turns[-1].created_at, current.turns[-1].budget) == original
        assert _readonly_state(actual.scenario) == before

    await _case(tmp_path, config, monkeypatch, inspect)
