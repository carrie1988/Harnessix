"""真实原认证 SDK 的 prepared 审批历史只读链；不执行 Git 业务效果。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.product_config.git_prepared_approval_history import (
    ProductGitPreparedApprovalHistoryReader,
)
from harnessix.protocol.contracts import ApprovalRespondParams, PublicApprovalDecision
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _history,
    _ledger,
    _rows,
)


def _reader(actual, database):
    """原连接与原资源进入新 Reader，不提交游离历史或调用方决定。"""
    return ProductGitPreparedApprovalHistoryReader(
        database,
        actual.scenario.router,
        actual.preparer.core_store,
        actual.artifacts,
        actual.scenario.reader,
        snapshot_ports=actual.scenario.router._snapshot_ports,
        workspace_scope=actual.provider._workspace_scope,
    )


async def _prepared(actual):
    with _database(actual) as database:
        await _genesis(actual, database)
        database.execute("BEGIN IMMEDIATE")
        link = await _ledger(actual, database).prepare(
            actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
        )
        database.execute("COMMIT")
        return link, _rows(database)


async def _decide(actual, monkeypatch, outcome):
    """正式 SDK 决定持久化后停在执行前；只禁止后台启动，不伪造审批或 Session。"""
    scenario = actual.scenario
    service = scenario.client.transport.server.service
    spawns = []

    def stop_before_execution(thread_id, turn_id):
        assert (thread_id, turn_id) == (actual.thread.thread_id, actual.turn.turn_id)
        spawns.append((thread_id, turn_id))

    monkeypatch.setattr(service, "_spawn", stop_before_execution)
    request = next(
        item.content
        for item in actual.turn.items
        if type(item.content) is TrustedActionApprovalRequestContent
    )
    await scenario.client.respond_approval(
        ApprovalRespondParams(
            request_id="history-" + outcome,
            thread_id=actual.thread.thread_id,
            turn_id=actual.turn.turn_id,
            approval_id=request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=PublicApprovalDecision(
                outcome=outcome, actor="history-test", reason="原请求范围核对"
            ),
        )
    )
    return spawns


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "cancelled"])
async def test_original_sdk_history_read_only_and_old_pending_reader_unchanged(
    tmp_path, config, monkeypatch, state
):
    async def inspect(actual):
        scenario = actual.scenario
        link, git_rows = await _prepared(actual)
        if state in {"approved", "denied"}:
            await _decide(actual, monkeypatch, "approved" if state == "approved" else "rejected")
        elif state == "cancelled":
            await scenario.client.cancel_turn(
                actual.thread.thread_id, actual.turn.turn_id, request_id="history-cancel"
            )
        history = await _history(actual)
        source = _source_snapshot(scenario.root)
        unchanged = scenario.unchanged_state()
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            result = await _reader(actual, database).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            )
            assert len(result) == 1
            assert result[0].prepared == link
            projection = result[0].approval_history
            assert projection.state == state
            assert projection.request_event in history.events
            assert result[0].linkage_state == (
                "prepared" if state == "pending" else "decision_not_linked"
            )
            assert not hasattr(result[0], "can_execute")
            assert database.total_changes == 0
            assert _rows(database) == git_rows
            database.execute("ROLLBACK")
            database.execute("BEGIN")
            # 正向 pending 回读由独立原 Turn 的 Ledger 重开情形覆盖。
            if state != "pending":
                with pytest.raises(KernelError):
                    await _ledger(actual, database).read_all(
                        cancel=CancelToken(), checkpoint=lambda: None
                    )
        assert _source_snapshot(scenario.root) == source
        assert await _history(actual) == history
        assert scenario.unchanged_state() == unchanged

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_original_router_first_window_rejected_then_original_sync_recovery(
    tmp_path, config, monkeypatch
):
    from harnessix.domain.models import ApprovalDecision, ApprovalOutcome

    async def inspect(actual):
        scenario = actual.scenario
        link, _git_rows = await _prepared(actual)
        runtime = scenario.client.transport.server.service.runtime
        actions = runtime._trusted_actions
        # 故意在原 Router/Execution 已提交而原 Session 尚未提交处停止。
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
            assert database.total_changes == 0
        assert await _history(actual) == before
        await actions.sync_decision(actual.thread.thread_id, actual.turn.turn_id)
        after_sync = await _history(actual)
        assert after_sync != before
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            result = await _reader(actual, database).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            )
            assert result[0].approval_history.state == "approved"
            assert result[0].linkage_state == "decision_not_linked"
            assert database.total_changes == 0
        assert await _history(actual) == after_sync

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_original_upstream_checkpoint_exception_instances_preserved(
    tmp_path, config, monkeypatch
):
    import sqlite3

    sentinels = (
        KernelError("git_approval_history_changed", "原控制异常"),
        OSError("original checkpoint"),
        sqlite3.OperationalError("original checkpoint"),
        TimeoutError("original checkpoint"),
    )

    async def inspect(actual):
        await _prepared(actual)
        for sentinel in sentinels:
            # 同一原宿主核对四种异常，无重复模型规划或重复准备。
            def checkpoint(original=sentinel):
                raise original

            with _database(actual, read_only=True) as database:
                database.execute("BEGIN")
                with pytest.raises(type(sentinel)) as caught:
                    await _reader(actual, database).read_all(
                        cancel=CancelToken(), checkpoint=checkpoint
                    )
                assert caught.value is sentinel
                assert database.total_changes == 0

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_last_upstream_callback_same_value_original_write_is_rejected(
    tmp_path, config, monkeypatch
):
    import harnessix.product_config.git_prepared_approval_history as module

    async def inspect(actual):
        scenario = actual.scenario
        await _prepared(actual)
        original = module._read_all
        complete = changed = False

        async def completed(*args, **kwargs):
            nonlocal complete
            result = await original(*args, **kwargs)
            complete = True
            return result

        monkeypatch.setattr(module, "_read_all", completed)
        original_changes = scenario.transactions._db.total_changes

        def checkpoint():
            nonlocal changed
            if complete and not changed:
                changed = True
                # 修改后字节相同，仍是原连接新写入，不能用仅行内容相等放过。
                scenario.transactions._db.execute(
                    "UPDATE workspace_transaction_events SET payload=payload"
                ).close()

        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await _reader(actual, database).read_all(
                    cancel=CancelToken(), checkpoint=checkpoint
                )
            assert caught.value.code == "git_prepared_link_changed"
            assert changed and scenario.transactions._db.total_changes > original_changes
            assert database.total_changes == 0

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_terminal_does_not_reenter_execution_shared_checkpoint(tmp_path, config, monkeypatch):
    from harnessix.workspace.terminal_read_control import terminal_parent_reader

    async def inspect(actual):
        scenario = actual.scenario
        await _prepared(actual)
        await _decide(actual, monkeypatch, "approved")
        plans = scenario.router._plans
        original = plans._checkpoint
        calls = 0

        def shared_checkpoint():
            nonlocal calls
            assert terminal_parent_reader(scenario.router._audit) is None
            calls += 1
            original()

        monkeypatch.setattr(plans, "_checkpoint", shared_checkpoint)
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            result = await _reader(actual, database).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            )
            assert result[0].approval_history.state == "approved"
            assert calls > 0
            assert database.total_changes == 0

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_all_original_git_mac_checked_before_history_interpretation(
    tmp_path, config, monkeypatch
):
    import harnessix.product_config.git_prepared_approval_history as module
    from harnessix.product_config.git_prefix_sql import git_prefix_sql_window

    async def inspect(actual):
        await _prepared(actual)
        with _database(actual) as database:
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                database.execute("BEGIN IMMEDIATE")
                database.execute("UPDATE git_product_links SET payload=payload || ' '")
                database.execute("COMMIT")

        async def must_not_interpret(*args, **kwargs):
            pytest.fail("全原 Git MAC 未通过前不得解释历史")

        monkeypatch.setattr(module, "read_original_approval_evidence", must_not_interpret)
        history = await _history(actual)
        with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            damaged = _rows(database)
            with pytest.raises(KernelError):
                await _reader(actual, database).read_all(
                    cancel=CancelToken(), checkpoint=lambda: None
                )
            assert _rows(database) == damaged
            assert database.total_changes == 0
        assert await _history(actual) == history

    await _case(tmp_path, config, monkeypatch, inspect)
