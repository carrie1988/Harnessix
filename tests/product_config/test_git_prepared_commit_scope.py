"""实际 SDK 的 prepare 返回至 caller COMMIT 边界；不证明原生 FD 或跨资源原子性。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_link_ledger as ledger_module
from harnessix.product_config import git_prepared_link_observation as observation
from harnessix.product_config.git_prepared_runtime_thread import prepared_git_commit_scope
from harnessix.product_config.git_user_source_scope import GitUserSourceScope
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_prepared_link_controls import _persisted
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _ledger,
    _rows,
)
from tests.product_config.test_git_prepared_link_terminal_callbacks import (
    _instrument_constructors,
)


def _capture_source_scopes(monkeypatch):
    scopes = []
    original = GitUserSourceScope.__enter__

    def entered(self):
        result = original(self)
        scopes.append(self)
        return result

    monkeypatch.setattr(GitUserSourceScope, "__enter__", entered)
    return scopes


async def test_actual_commit_retains_sources_without_callbacks_and_reopens(
    tmp_path, config, monkeypatch
):
    _, calls, phase, terminal_calls = _instrument_constructors(monkeypatch)
    scopes = _capture_source_scopes(monkeypatch)

    async def inspect(actual):
        async with _database(actual) as db:
            await _genesis(actual, db)
            db.execute("BEGIN IMMEDIATE")
            ledger = _ledger(actual, db)
            link = await ledger.prepare(
                actual.route.plan.execution.plan_id,
                cancel=CancelToken(),
                checkpoint=lambda: None,
            )
            held = scopes[-1]
            assert held._active and held._sources is not None
            before = calls.copy()
            phase["terminal"] = True
            try:
                with prepared_git_commit_scope(db):
                    assert held._active and db.in_transaction
                    db.execute("COMMIT")
            finally:
                phase["terminal"] = False
            assert calls == before and not terminal_calls
            assert not db.in_transaction and not held._active
            assert held._sources is None
            committed = _rows(db)
            with pytest.raises(KernelError) as caught:
                with prepared_git_commit_scope(db):
                    pytest.fail("提交候选只能消费一次")
            assert caught.value.code == "git_runtime_thread_scope_invalid"
        async with _database(actual, read_only=True) as db:
            db.execute("BEGIN")
            assert await _ledger(actual, db).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            ) == (link,)
            assert _rows(db) == committed and db.total_changes == 0

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("body_error", [False, True])
async def test_commit_scope_never_controls_caller_transaction(
    tmp_path, config, monkeypatch, body_error
):
    scopes = _capture_source_scopes(monkeypatch)

    async def inspect(actual):
        async with _database(actual) as db:
            await _genesis(actual, db)
            baseline = _rows(db)
            db.execute("BEGIN IMMEDIATE")
            with pytest.raises(KernelError) as caught:
                with prepared_git_commit_scope(db):
                    pytest.fail("未完成 prepare 不能取得提交入口")
            assert caught.value.code == "git_runtime_thread_scope_invalid"
            await _ledger(actual, db).prepare(
                actual.route.plan.execution.plan_id,
                cancel=CancelToken(),
                checkpoint=lambda: None,
            )
            marker = OSError("original-caller-failure")
            if body_error:
                with pytest.raises(OSError) as caught:
                    with prepared_git_commit_scope(db):
                        raise marker
                assert caught.value is marker
            else:
                with prepared_git_commit_scope(db):
                    assert db.in_transaction
            assert db.in_transaction and not scopes[-1]._active
            assert _rows(db) != baseline
            db.execute("ROLLBACK")
            assert _rows(db) == baseline
        await _persisted(actual, baseline)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_same_transaction_retry_replaces_one_live_source_set(tmp_path, config, monkeypatch):
    scopes = _capture_source_scopes(monkeypatch)

    async def inspect(actual):
        async with _database(actual) as db:
            await _genesis(actual, db)
            db.execute("BEGIN IMMEDIATE")
            ledger = _ledger(actual, db)
            route_id = actual.route.plan.execution.plan_id
            first = await ledger.prepare(route_id, cancel=CancelToken(), checkpoint=lambda: None)
            held = scopes[-1]
            rows, changes = _rows(db), db.total_changes
            assert (
                await ledger.prepare(route_id, cancel=CancelToken(), checkpoint=lambda: None)
                == first
            )
            assert not held._active and scopes[-1]._active
            assert sum(scope._active for scope in scopes) == 1
            assert _rows(db) == rows and db.total_changes == changes
            with prepared_git_commit_scope(db):
                db.execute("COMMIT")
            assert not any(scope._active for scope in scopes)

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["config-value", "same-oid-head", "rollback-begin", "savepoint"])
async def test_actual_gap_drift_rejects_before_caller_commit(tmp_path, config, monkeypatch, fault):
    scopes = _capture_source_scopes(monkeypatch)

    async def inspect(actual):
        root = actual.scenario.root
        if fault == "same-oid-head":
            oid = command(root, "rev-parse", "HEAD").strip().decode()
            command(root, "update-ref", "refs/heads/commit-gap-alternative", oid)
        async with _database(actual) as db:
            await _genesis(actual, db)
            baseline = _rows(db)
            db.execute("BEGIN IMMEDIATE")
            await _ledger(actual, db).prepare(
                actual.route.plan.execution.plan_id,
                cancel=CancelToken(),
                checkpoint=lambda: None,
            )
            held = scopes[-1]
            if fault == "config-value":
                command(root, "config", "user.name", "Commit Gap Drift")
            elif fault == "same-oid-head":
                command(root, "symbolic-ref", "HEAD", "refs/heads/commit-gap-alternative")
            elif fault == "rollback-begin":
                db.execute("ROLLBACK")
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("SAVEPOINT gap")
                db.execute("RELEASE gap")
            changes, uncommitted = db.total_changes, _rows(db)
            with pytest.raises(KernelError) as caught:
                with prepared_git_commit_scope(db):
                    pytest.fail("漂移必须在调用方 COMMIT 之前拒绝")
            expected = (
                "publication_history_unproven"
                if fault in {"rollback-begin", "savepoint"}
                else "git_user_observation_changed"
            )
            assert caught.value.code == expected
            assert db.in_transaction and _rows(db) == uncommitted
            assert db.total_changes == changes and not held._active
            db.execute("ROLLBACK")
            assert _rows(db) == baseline
        await _persisted(actual, baseline)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_foreign_task_and_event_loop_callback_cannot_consume_commit_record(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        async with _database(actual) as db:
            await _genesis(actual, db)
            db.execute("BEGIN IMMEDIATE")
            await _ledger(actual, db).prepare(
                actual.route.plan.execution.plan_id,
                cancel=CancelToken(),
                checkpoint=lambda: None,
            )

            def rejected():
                with pytest.raises(KernelError) as caught:
                    with prepared_git_commit_scope(db):
                        pytest.fail("提交候选不向其他 Task 或无 Task 回调授权")
                assert caught.value.code == "git_runtime_thread_scope_invalid"

            async def child():
                rejected()

            await asyncio.create_task(child())
            future = asyncio.get_running_loop().create_future()

            def callback():
                try:
                    rejected()
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(None)

            asyncio.get_running_loop().call_soon(callback)
            await future
            with prepared_git_commit_scope(db):
                db.execute("COMMIT")
            assert not db.in_transaction

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["cancel", "deadline", "terminal-error", "owner-revoked"])
async def test_original_control_failure_invalidates_commit_record_and_rolls_back(
    tmp_path, config, monkeypatch, fault
):
    scopes = _capture_source_scopes(monkeypatch)
    budgets = []
    budget_type = ledger_module.GitOperationBudget

    def captured_budget(seconds):
        budget = budget_type(seconds)
        budgets.append(budget)
        return budget

    monkeypatch.setattr(ledger_module, "GitOperationBudget", captured_budget)

    async def inspect(actual):
        async with _database(actual) as db:
            await _genesis(actual, db)
            baseline = _rows(db)
            db.execute("BEGIN IMMEDIATE")
            cancel = CancelToken()
            await _ledger(actual, db).prepare(
                actual.route.plan.execution.plan_id, cancel=cancel, checkpoint=lambda: None
            )
            held = scopes[-1]
            marker = ValueError("original-terminal-failure")
            saved = None
            snapshot = None
            audit = actual.scenario.router._audit
            with monkeypatch.context() as patch:
                if fault == "cancel":
                    cancel.cancel()
                elif fault == "deadline":
                    # 直接推进原预算的绝对期限；不修改正式时限或构造替代控制。
                    budgets[-1]._deadline = 0
                elif fault == "terminal-error":

                    def failed(*_args, **_kwargs):
                        raise marker

                    patch.setattr(observation.PreparedLinkReadSet, "terminal", failed)
                else:
                    saved = audit._db.execute(
                        "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
                    ).fetchone()[0]
                    snapshot = audit._db.execute(
                        "SELECT key,value FROM action_audit_metadata ORDER BY key"
                    )
                    assert snapshot.fetchone() is not None
                    with closing(sqlite3.connect(audit._path)) as external, external:
                        external.execute(
                            "UPDATE action_audit_metadata SET value='revoked' "
                            "WHERE key='owner_generation'"
                        )
                    assert audit._db.execute(
                        "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
                    ).fetchone() == (saved,)
                error_type = (
                    TurnCancelled
                    if fault == "cancel"
                    else (ValueError if fault == "terminal-error" else KernelError)
                )
                try:
                    with pytest.raises(error_type) as caught:
                        with prepared_git_commit_scope(db):
                            pytest.fail("原控制失败不得到达 COMMIT")
                    if fault == "terminal-error":
                        assert caught.value is marker
                    elif fault != "cancel":
                        assert caught.value.code == (
                            "git_process_timeout"
                            if fault == "deadline"
                            else "action_runtime_fence_lost"
                        )
                finally:
                    if snapshot is not None:
                        snapshot.close()
                    if saved is not None:
                        audit._db.execute(
                            "UPDATE action_audit_metadata SET value=? WHERE key='owner_generation'",
                            (saved,),
                        )
            assert db.in_transaction and not held._active
            with pytest.raises(KernelError) as caught:
                with prepared_git_commit_scope(db):
                    pytest.fail("失败的候选不可重用")
            assert caught.value.code == "git_runtime_thread_scope_invalid"
            db.execute("ROLLBACK")
            assert _rows(db) == baseline
        await _persisted(actual, baseline)

    await _case(tmp_path, config, monkeypatch, inspect)
