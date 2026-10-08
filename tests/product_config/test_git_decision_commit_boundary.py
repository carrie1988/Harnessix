"""正式决定返回至调用方提交间的负控；不证明外部 ABA 连续性或跨库原子性。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome
from harnessix.product_config.git_decision_link_ledger import ProductGitDecisionLinkLedger
from harnessix.product_config.git_prepared_runtime_thread import prepared_git_commit_scope
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_prepared_commit_scope import _capture_source_scopes
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _history,
    _ledger,
    _rows,
)
from tests.support.git_decision_recovery import router_first


@pytest.mark.parametrize(
    "fault", ["config-value", "same-oid-head", "rollback-begin", "savepoint", "cancel", "deadline"]
)
async def test_decision_commit_rejects_gap_drift_and_keeps_caller_transaction(
    tmp_path, config, monkeypatch, fault
):
    scopes = _capture_source_scopes(monkeypatch)
    budgets = []
    original_append = ProductGitDecisionLinkLedger._append_decision

    async def capture_budget(self, *args, **kwargs):
        budgets.append(kwargs["budget"])
        return await original_append(self, *args, **kwargs)

    monkeypatch.setattr(ProductGitDecisionLinkLedger, "_append_decision", capture_budget)

    async def inspect(actual):
        root = actual.scenario.root
        alternative = "refs/heads/decision-commit-alternative"
        if fault == "same-oid-head":
            oid = command(root, "rev-parse", "HEAD").strip()
            command(root, "update-ref", alternative, oid.decode())
        _runtime, actions, baseline = await router_first(actual, ApprovalOutcome.APPROVED)
        async with _database(actual) as database:
            await actions._sync_decision_in_owned_thread(
                actual.thread.thread_id, actual.turn.turn_id
            )
            history = await _history(actual)
            database.execute("BEGIN IMMEDIATE")
            token = CancelToken()
            result = await ProductGitDecisionLinkLedger(_ledger(actual, database)).append_decision(
                actual.route.plan.execution.plan_id, cancel=token, checkpoint=lambda: None
            )
            assert result.fact_kind == "approved" and database.in_transaction
            held = scopes[-1]
            assert held._active and held._sources is not None
            if fault == "config-value":
                command(root, "config", "user.name", "Decision Commit Drift")
            elif fault == "same-oid-head":
                command(root, "symbolic-ref", "HEAD", alternative)
                assert command(root, "rev-parse", "HEAD").strip() == oid
            elif fault == "rollback-begin":
                database.execute("ROLLBACK")
                database.execute("BEGIN IMMEDIATE")
            elif fault == "savepoint":
                database.execute("SAVEPOINT gap")
                database.execute("RELEASE gap")
            elif fault == "cancel":
                token.cancel()
            else:
                assert len(budgets) == 1 and budgets[0].remaining() <= 60
                budgets[0]._deadline = 0.0
            rows, changes = _rows(database), database.total_changes
            with pytest.raises(TurnCancelled if fault == "cancel" else KernelError) as caught:
                with prepared_git_commit_scope(database):
                    pytest.fail("决定事实不能在来源或原控制漂移后提交")
            if fault != "cancel":
                assert caught.value.code == (
                    "publication_history_unproven"
                    if fault in {"rollback-begin", "savepoint"}
                    else (
                        "git_process_timeout"
                        if fault == "deadline"
                        else "git_user_observation_changed"
                    )
                )
            assert database.in_transaction and _rows(database) == rows
            assert database.total_changes == changes and not held._active
            database.execute("ROLLBACK")
            assert _rows(database) == baseline and await _history(actual) == history
        async with _database(actual, read_only=True) as reopened:
            assert _rows(reopened) == baseline and reopened.total_changes == 0

    await _case(tmp_path, config, monkeypatch, inspect)
