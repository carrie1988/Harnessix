"""原认证 SDK 的恢复屏障：保留原决定、事务所有权及读取失败的首异常。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import partial

import pytest

from harnessix.agent.approvals import approval_for
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.product_config import git_decision_recovery as recovery
from harnessix.product_config.git_decision_link_ledger import ProductGitDecisionLinkLedger
from harnessix.product_config.git_prefix_sql import _dispatch_git_prefix_trace
from tests.product_config.test_git_prepared_approval_history import _reader
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _history,
    _ledger,
    _rows,
)
from tests.support.git_decision_recovery import recover_original_decision, router_first


@pytest.mark.parametrize("outcome", [ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED])
async def test_recovery_commits_original_fact_and_exact_retry_has_no_dml(
    tmp_path, config, monkeypatch, outcome
):
    async def inspect(actual):
        runtime, _actions, baseline = await router_first(actual, outcome)
        before = await _history(actual)
        effects = actual.scenario.unchanged_state()
        async with _database(actual) as database:
            lock = runtime._locks[actual.thread.thread_id]
            owner, generation = lock._owner, lock._owner_generation
            fact = await recover_original_decision(actual, database)
            committed = _rows(database)
            assert committed != baseline and not database.in_transaction
            after = await _history(actual)
            assert len(after.events) == len(before.events) + 1
            changes = database.total_changes
            duplicate = await recover_original_decision(actual, database)
            assert duplicate == fact and database.total_changes == changes
            assert _rows(database) == committed and await _history(actual) == after
            assert lock._owner is owner and lock._owner_generation == generation
            assert fact.session_decision.decision.outcome == outcome
        async with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            reopened = await _reader(actual, database).read_linked_decision(
                actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert reopened == fact and database.total_changes == 0
        assert actual.scenario.unchanged_state() == effects

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_original_host_failure_precedes_existing_transaction_rejection(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        before = await _history(actual)
        async with _database(actual) as database:
            await _genesis(actual, database)
            baseline, changes = _rows(database), database.total_changes
            resources = _ledger(actual, database)
            resources._router = object()
            database.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await recovery.recover_decision_link(
                    ProductGitDecisionLinkLedger(resources),
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
            # 复合失效先报原宿主身份错误；不得代管调用方已有事务或同步 Session。
            assert caught.value.code == "git_runtime_thread_scope_invalid"
            assert database.in_transaction and database.total_changes == changes
            database.execute("ROLLBACK")
            assert _rows(database) == baseline and await _history(actual) == before

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "cas",
        "task_cancel",
        "post_sync_cancel",
        "append_failure",
        "existing_transaction",
        "other_task",
        "deadline_before_sync",
        "deadline_after_sync",
        "append_cancel",
        "append_timeout",
        "transaction_replaced",
    ],
)
async def test_recovery_failure_boundaries_preserve_original_authority(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(actual):
        _runtime, actions, baseline = await router_first(actual, ApprovalOutcome.APPROVED)
        before = await _history(actual)
        token = CancelToken()
        sentinel = (
            asyncio.CancelledError("original-task-cancel")
            if fault == "task_cancel"
            else (
                KernelError("session_conflict", "original-CAS-failure")
                if fault == "cas"
                else RuntimeError("original-append-failure")
            )
        )
        async with _database(actual) as database:
            original_sync = actions._sync_decision_in_owned_thread
            original_append = ProductGitDecisionLinkLedger._append
            source_append = actual.scenario.session.append
            sync_done = False
            seen = []
            replacement_epoch = None
            shared_budget = None
            original_decision_append = ProductGitDecisionLinkLedger._append_decision
            original_remaining = recovery.remaining_seconds

            def remaining(turn):
                if fault == "deadline_before_sync" or (
                    fault == "deadline_after_sync" and sync_done
                ):
                    return 0.0
                return original_remaining(turn)

            async def borrowed_append(self, *args, **kwargs):
                nonlocal shared_budget
                shared_budget = kwargs["budget"]
                assert shared_budget.remaining() <= 60
                return await original_decision_append(self, *args, **kwargs)

            def observe_sql(sql):
                _dispatch_git_prefix_trace(database, sql)
                seen.append(sql)

            database.set_trace_callback(observe_sql)

            async def failed_cas(*args, **kwargs):
                assert not database.in_transaction
                raise sentinel

            async def observed_sync(*args, **kwargs):
                nonlocal sync_done
                value = await original_sync(*args, **kwargs)
                sync_done = True
                return value

            async def failed_append(*args, **kwargs):
                nonlocal replacement_epoch
                await original_append(*args, **kwargs)
                if fault == "append_cancel":
                    token.cancel()
                    return
                if fault == "append_timeout":
                    assert shared_budget is not None
                    # 原调用借用的同一预算到期，不能靠另建预算继续提交。
                    shared_budget._deadline = 0.0
                    return
                if fault == "transaction_replaced":
                    original_epoch = recovery._git_prefix_caller_transaction_epoch(database)
                    database.execute("ROLLBACK")
                    database.execute("BEGIN")
                    replacement_epoch = recovery._git_prefix_caller_transaction_epoch(database)
                    assert replacement_epoch[0] is original_epoch[0]
                    assert replacement_epoch[1] > original_epoch[1]
                raise sentinel

            def checkpoint():
                if fault == "post_sync_cancel" and sync_done:
                    token.cancel()

            with monkeypatch.context() as injection:
                injection.setattr(actions, "_sync_decision_in_owned_thread", observed_sync)
                injection.setattr(ProductGitDecisionLinkLedger, "_append_decision", borrowed_append)
                if fault.startswith("deadline_"):
                    injection.setattr(recovery, "remaining_seconds", remaining)
                if fault in {"cas", "task_cancel"}:
                    injection.setattr(actual.scenario.session, "append", failed_cas)
                if fault in {
                    "append_failure",
                    "append_cancel",
                    "append_timeout",
                    "transaction_replaced",
                }:
                    injection.setattr(ProductGitDecisionLinkLedger, "_append", failed_append)
                if fault == "existing_transaction":
                    database.execute("BEGIN")
                expected = (
                    TurnCancelled
                    if fault in {"post_sync_cancel", "append_cancel"}
                    else (
                        KernelError
                        if fault
                        in {
                            "existing_transaction",
                            "other_task",
                            "deadline_before_sync",
                            "deadline_after_sync",
                            "append_timeout",
                        }
                        else type(sentinel)
                    )
                )
                with pytest.raises(expected) as caught:
                    operation = recover_original_decision(
                        actual, database, token=token, check=checkpoint
                    )
                    if fault == "other_task":
                        await asyncio.create_task(operation)
                    else:
                        await operation
                if fault in {"cas", "task_cancel", "append_failure", "transaction_replaced"}:
                    assert caught.value is sentinel
            assert actual.scenario.session.append == source_append
            if fault in {"existing_transaction", "transaction_replaced"}:
                # 恢复屏障无权回滚调用方已有事务或失败注入后新建的替换事务。
                assert database.in_transaction
                if fault == "transaction_replaced":
                    assert (
                        recovery._git_prefix_caller_transaction_epoch(database) == replacement_epoch
                    )
                database.execute("ROLLBACK")
            else:
                assert not database.in_transaction
            assert _rows(database) == baseline
            history = await _history(actual)
            delta = len(history.events) - len(before.events)
            after_sync_faults = {
                "post_sync_cancel",
                "append_failure",
                "deadline_after_sync",
                "append_cancel",
                "append_timeout",
                "transaction_replaced",
            }
            assert delta == (1 if fault in after_sync_faults else 0)
            if fault not in {
                "append_failure",
                "append_cancel",
                "append_timeout",
                "transaction_replaced",
            }:
                expected_begin = 1 if fault == "existing_transaction" else 0
                assert sum(sql.startswith("BEGIN") for sql in seen) == expected_begin
            # 移除测试记录器时仍保留事务代际 dispatcher，不能清空 trace callback。
            database.set_trace_callback(partial(_dispatch_git_prefix_trace, database))
            # 只复用原权威，不重新 decide；原已提交 Session 决定不得再追加。
            fact = await recover_original_decision(actual, database)
            assert fact.session_decision.decision.outcome is ApprovalOutcome.APPROVED
            assert len((await _history(actual)).events) == len(before.events) + 1

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_commit_confirmation_loss_is_readback_not_blind_new_decision(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        _runtime, _actions, baseline = await router_first(actual, ApprovalOutcome.APPROVED)
        before = await _history(actual)
        sentinel = RuntimeError("lost-commit-confirmation")
        original_scope = recovery.prepared_git_commit_scope
        async with _database(actual) as database:

            @contextmanager
            def lost_confirmation(connection):
                with original_scope(connection):
                    yield
                assert not connection.in_transaction
                raise sentinel

            with monkeypatch.context() as injection:
                injection.setattr(recovery, "prepared_git_commit_scope", lost_confirmation)
                with pytest.raises(RuntimeError) as caught:
                    await recover_original_decision(actual, database)
                assert caught.value is sentinel
            committed = _rows(database)
            assert committed != baseline and not database.in_transaction
            history = await _history(actual)
            assert len(history.events) == len(before.events) + 1
            changes = database.total_changes
            fact = await recover_original_decision(actual, database)
            assert database.total_changes == changes and _rows(database) == committed
            assert fact.session_decision.decision.outcome is ApprovalOutcome.APPROVED
            assert await _history(actual) == history

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_missing_original_prepared_target_cannot_sync_session(tmp_path, config, monkeypatch):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        actions = runtime._trusted_actions
        item = approval_for(actual.turn, actual.call)
        assert item is not None
        actions._state.gateway.decide(
            actual.thread,
            actual.turn,
            actual.call,
            item.content,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="missing-original-target"),
        )
        before = await _history(actual)
        async with _database(actual) as database:
            await _genesis(actual, database)
            baseline, changes = _rows(database), database.total_changes
            with pytest.raises(KernelError) as caught:
                await recover_original_decision(actual, database)
            assert caught.value.code == "git_decision_recovery_scope_invalid"
            assert _rows(database) == baseline and database.total_changes == changes
            assert not database.in_transaction
            assert await _history(actual) == before

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["close_and_raise", "raise_only"])
async def test_readonly_source_cleanup_preserves_original_failure(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(actual):
        await router_first(actual, ApprovalOutcome.APPROVED)
        before = await _history(actual)
        sentinel = KernelError("original_private_reader_failed", "合成原读取首异常")
        async with _database(actual) as database:
            baseline, changes = _rows(database), database.total_changes

            def failed_read(source, *args, **kwargs):
                assert source is not database and source.in_transaction
                assert source.execute("PRAGMA query_only").fetchone() == (1,)
                if fault == "close_and_raise":
                    source.close()
                raise sentinel

            with monkeypatch.context() as injected:
                injected.setattr(recovery, "read_git_link_history_rows", failed_read)
                with pytest.raises(KernelError) as caught:
                    await recover_original_decision(actual, database)
                assert caught.value is sentinel
            assert not database.in_transaction and _rows(database) == baseline
            assert database.total_changes == changes and await _history(actual) == before

    await _case(tmp_path, config, monkeypatch, inspect)
