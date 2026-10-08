"""原用户观察返回后的 Git 来源漂移必须拒绝；两个只读消费者均不产生新写入。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_prepared_approval_history as history_module
from harnessix.product_config import git_prepared_link_ledger as ledger_module
from harnessix.product_config import git_user_observation as observation_module
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_link_user_observation_consumption import _consume
from tests.product_config.test_git_prepared_approval_history import _prepared
from tests.product_config.test_git_prepared_link_ledger import _case, _database, _rows
from tests.product_config.test_git_user_observation_verification import _readonly_state


def _observe_original_u_return(patch, operation):
    """始终等待原验证器；完成标记仅在真实原观察复核成功返回后设置。"""
    module = ledger_module if operation == "pending-read" else history_module
    original = module.verify_product_git_user_observation
    observed = SimpleNamespace(original_calls=0, finished_u=False)

    async def verified(*args, **kwargs):
        observed.original_calls += 1
        result = await original(*args, **kwargs)
        observed.finished_u = True
        return result

    patch.setattr(module, "verify_product_git_user_observation", verified)
    return observed


def _forbid_new_observation_or_effects(patch, scenario):
    """回读只能复核既有观察；重捕、CAS 写入、执行或对账均直接失败。"""

    def forbidden(*_args, **_kwargs):
        pytest.fail("既有观察回读不得重捕来源、写入 CAS、执行或对账")

    for name in (
        "collect_product_git_user_observation",
        "collect_git_delivery_source",
        "_collect_baseline_from_source",
    ):
        patch.setattr(observation_module, name, forbidden)
    patch.setattr(SQLiteWorkspaceTransactionStore, "_put_blob", forbidden)
    patch.setattr(scenario.router, "execute", forbidden)
    patch.setattr(scenario.router, "reconcile", forbidden)


@pytest.mark.parametrize("operation", ["pending-read", "history-read"])
@pytest.mark.parametrize("fault", ["config-value", "same-oid-head-ref"])
async def test_after_original_u_return_source_drift_rejects_read_all_without_writes(
    tmp_path, config, monkeypatch, operation, fault
):
    async def inspect(actual):
        scenario = actual.scenario
        _, sealed = await _prepared(actual)
        head_oid = command(scenario.root, "rev-parse", "HEAD").strip()
        head_ref = command(scenario.root, "symbolic-ref", "HEAD").strip()
        alternative = "refs/heads/terminal-alternative"
        drift_name = "Terminal Source Drift"
        if fault == "same-oid-head-ref":
            assert head_ref != alternative.encode()
            command(scenario.root, "update-ref", alternative, head_oid.decode())
        config_keys = command(scenario.root, "config", "--no-includes", "--name-only", "--list")
        original_name = command(scenario.root, "config", "user.name")
        assert original_name.strip() != drift_name.encode()
        before = _readonly_state(scenario)
        injections = 0
        injected_source = None

        with monkeypatch.context() as patch:
            observed = _observe_original_u_return(patch, operation)
            _forbid_new_observation_or_effects(patch, scenario)

            def checkpoint():
                nonlocal injections, injected_source
                if observed.finished_u and injections == 0:
                    if fault == "config-value":
                        command(scenario.root, "config", "user.name", drift_name)
                    else:
                        command(scenario.root, "symbolic-ref", "HEAD", alternative)
                    injections += 1
                    # 仅记录临时仓库物理状态，不重捕或签发产品用户观察。
                    injected_source = _source_snapshot(scenario.root)

            # 使用可写连接观测总写次数，避免只读连接替代消费者的零写保证。
            async with _database(actual) as database:
                database.execute("BEGIN")
                assert _rows(database) == sealed
                changes = database.total_changes
                with pytest.raises(KernelError) as caught:
                    await _consume(
                        actual, database, operation, cancel=CancelToken(), checkpoint=checkpoint
                    )
                assert observed.original_calls >= 1 and observed.finished_u
                assert injections == 1 and injected_source is not None
                assert caught.value.code == "git_user_observation_changed"
                # 回滚前先证明全部行和总写次数不变，回滚不能掩盖消费者写入。
                assert _rows(database) == sealed and database.total_changes == changes
                assert _readonly_state(scenario) == before
                assert _source_snapshot(scenario.root) == injected_source
                assert (
                    command(scenario.root, "config", "--no-includes", "--name-only", "--list")
                    == config_keys
                )
                assert command(scenario.root, "rev-parse", "HEAD").strip() == head_oid
                if fault == "config-value":
                    assert (
                        command(scenario.root, "config", "user.name").strip() == drift_name.encode()
                    )
                    assert command(scenario.root, "symbolic-ref", "HEAD").strip() == head_ref
                else:
                    assert command(scenario.root, "config", "user.name") == original_name
                    assert (
                        command(scenario.root, "symbolic-ref", "HEAD").strip()
                        == alternative.encode()
                    )
                database.execute("ROLLBACK")
                assert not database.in_transaction
                assert _rows(database) == sealed and database.total_changes == changes

        async with _database(actual, read_only=True) as database:
            assert _rows(database) == sealed and database.total_changes == 0
        assert _readonly_state(scenario) == before
        # 保留已注入的真实漂移，不通过测试恢复配置或 HEAD 来获得完整性断言。
        assert _source_snapshot(scenario.root) == injected_source

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("operation", ["pending-read", "history-read"])
async def test_stable_terminal_sources_allow_each_read_all_without_new_writes(
    tmp_path, config, monkeypatch, operation
):
    async def inspect(actual):
        scenario = actual.scenario
        prepared, sealed = await _prepared(actual)
        before = _readonly_state(scenario)
        source = _source_snapshot(scenario.root)
        async with _database(actual) as database:
            with monkeypatch.context() as patch:
                observed = _observe_original_u_return(patch, operation)
                _forbid_new_observation_or_effects(patch, scenario)
                database.execute("BEGIN")
                changes = database.total_changes
                result = await _consume(
                    actual,
                    database,
                    operation,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
                assert observed.original_calls >= 1 and observed.finished_u
                if operation == "pending-read":
                    assert result == (prepared,)
                else:
                    assert len(result) == 1 and result[0].prepared == prepared
                    assert result[0].approval_history.state == "pending"
                    assert result[0].linkage_state == "prepared"
                assert _rows(database) == sealed and database.total_changes == changes
                assert _readonly_state(scenario) == before
                assert _source_snapshot(scenario.root) == source
                database.execute("ROLLBACK")
                assert not database.in_transaction
                assert _rows(database) == sealed and database.total_changes == changes
        async with _database(actual, read_only=True) as database:
            assert _rows(database) == sealed and database.total_changes == 0
        assert _readonly_state(scenario) == before
        assert _source_snapshot(scenario.root) == source

    await _case(tmp_path, config, monkeypatch, inspect)
