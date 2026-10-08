"""原连接事务代际的合作式观察；不授予 SQL 或发布窗口权限。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prefix_sql as sql
from harnessix.product_config.git_prefix_sql import (
    _git_prefix_caller_transaction_epoch,
    _git_prefix_transaction_scope,
    _register_git_prefix_write_window,
    _require_git_prefix_caller_transaction_epoch,
    _require_issued_git_prefix_window,
    git_prefix_sql_window,
    git_prefix_transaction_epoch,
    require_git_prefix_sql_window,
    require_git_prefix_transaction_epoch,
)
from harnessix.product_config.git_prepared_link_connection import open_prepared_git_connection

_BUSY_SQL = (
    "WITH RECURSIVE numbers(x) AS (VALUES(0) UNION ALL "
    "SELECT x+1 FROM numbers WHERE x<2000) SELECT SUM(x) FROM numbers"
)


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "ordinary.db"
    with closing(sqlite3.connect(path)) as seed:
        seed.execute("CREATE TABLE source (value TEXT)").close()
    return path


def test_scope_requires_factory_source_and_active_transaction(database_path):
    with closing(sqlite3.connect(database_path)) as generic:
        with pytest.raises(KernelError) as caught:
            with _git_prefix_transaction_scope(generic):
                pytest.fail("通用连接不能安装产品事务观察")
        assert caught.value.code == "git_prefix_sql_owner_invalid"

    with open_prepared_git_connection(database_path, read_only=False) as database:
        database.execute("BEGIN").close()
        for read in (
            lambda: _git_prefix_caller_transaction_epoch(database),
            lambda: _require_git_prefix_caller_transaction_epoch(database, (object(), 0)),
        ):
            with pytest.raises(KernelError) as caught:
                read()
            assert caught.value.code == "publication_history_unproven"
        database.rollback()
        with _git_prefix_transaction_scope(database):
            with pytest.raises(KernelError) as caught:
                _git_prefix_caller_transaction_epoch(database)
            assert caught.value.code == "publication_history_unproven"
            database.execute("BEGIN").close()
            epoch = _git_prefix_caller_transaction_epoch(database)
            database.execute("SELECT 'COMMIT', 'ROLLBACK' /* SAVEPOINT */").close()
            database.execute("INSERT INTO source VALUES ('private SQL body')").close()
            assert _git_prefix_caller_transaction_epoch(database) == epoch
            assert set(vars(epoch[0])) == {"epoch", "prepared_source", "task"}
            database.commit()
            with pytest.raises(KernelError) as caught:
                _require_git_prefix_caller_transaction_epoch(database, epoch)
            assert caught.value.code == "publication_history_unproven"


@pytest.mark.parametrize(
    "statements",
    [
        ("ROLLBACK", "BEGIN"),
        ("COMMIT", "BEGIN"),
        ("END", "BEGIN"),
        ("SAVEPOINT step", "RELEASE step"),
        ("SAVEPOINT step", "ROLLBACK TO step", "RELEASE step"),
    ],
)
def test_method_boundaries_invalidate_epoch_with_leading_comments(database_path, statements):
    with open_prepared_git_connection(database_path, read_only=False) as database:
        with _git_prefix_transaction_scope(database):
            database.execute("BEGIN").close()

            def observe():
                return _git_prefix_caller_transaction_epoch(database)

            before = observe()
            for statement in statements:
                database.execute("\ufeff ; -- 前导注释\n /* 事务边界 */\n " + statement).close()
            after = observe()
            assert after == (before[0], before[1] + len(statements))
            with pytest.raises(KernelError) as caught:
                _require_git_prefix_caller_transaction_epoch(database, before)
            assert caught.value.code == "publication_history_unproven"
            _require_git_prefix_caller_transaction_epoch(database, after)


def test_long_trace_survives_short_window_gap_without_owner_callbacks(database_path):
    checks = []
    with open_prepared_git_connection(database_path, read_only=False) as database:
        with _git_prefix_transaction_scope(database):
            database.execute("BEGIN").close()
            caller = _git_prefix_caller_transaction_epoch(database)
            with git_prefix_sql_window(database, checkpoint=lambda: checks.append(None)):
                short = git_prefix_transaction_epoch(database)
                database.execute("SAVEPOINT step").close()
                database.execute("RELEASE step").close()
                assert git_prefix_transaction_epoch(database) == (short[0], short[1] + 2)
                assert _git_prefix_caller_transaction_epoch(database) == (caller[0], caller[1] + 2)
            caller = _git_prefix_caller_transaction_epoch(database)
            count = len(checks)
            database.execute(_BUSY_SQL).fetchone()
            assert _git_prefix_caller_transaction_epoch(database) == caller
            _require_git_prefix_caller_transaction_epoch(database, caller)
            assert len(checks) == count
            database.rollback()
            database.execute("BEGIN").close()
            with git_prefix_sql_window(database, checkpoint=lambda: checks.append(None)):
                next_short = git_prefix_transaction_epoch(database)
                assert next_short[0] is not short[0]
                assert next_short[1] == 0
                assert _git_prefix_caller_transaction_epoch(database) == (caller[0], caller[1] + 2)
                with pytest.raises(KernelError):
                    _require_git_prefix_caller_transaction_epoch(database, caller)
                with pytest.raises(KernelError):
                    require_git_prefix_transaction_epoch(database, short)


@pytest.mark.parametrize("failure", ["body", "terminal", "progress"])
def test_short_failure_restores_long_trace_and_removes_progress(database_path, failure):
    marker = KernelError("original_failure", "原短窗口首失败")
    checks = []
    armed = False

    def checkpoint():
        checks.append(None)
        if armed and failure != "body":
            raise marker

    with open_prepared_git_connection(database_path, read_only=False) as database:
        with _git_prefix_transaction_scope(database):
            database.execute("BEGIN").close()
            caller = _git_prefix_caller_transaction_epoch(database)
            with pytest.raises(KernelError) as caught:
                with git_prefix_sql_window(database, checkpoint=checkpoint):
                    armed = True
                    if failure == "body":
                        raise marker
                    if failure == "progress":
                        database.execute(_BUSY_SQL).fetchone()
            assert caught.value is marker
            count = len(checks)
            database.execute(_BUSY_SQL).fetchone()
            database.execute("SAVEPOINT recovered").close()
            database.execute("RELEASE recovered").close()
            assert _git_prefix_caller_transaction_epoch(database) == (caller[0], caller[1] + 2)
            assert len(checks) == count
            with pytest.raises(KernelError) as caught:
                require_git_prefix_sql_window(database)
            assert caught.value.code == "git_prefix_sql_control_required"


def test_scope_does_not_authorize_sql_or_write_windows(database_path):
    with open_prepared_git_connection(database_path, read_only=False) as database:
        with _git_prefix_transaction_scope(database):
            database.execute("BEGIN").close()
            caller = _git_prefix_caller_transaction_epoch(database)
            for consume in (
                lambda: require_git_prefix_sql_window(database),
                lambda: git_prefix_transaction_epoch(database),
                lambda: _register_git_prefix_write_window(database, object()),
                lambda: _require_issued_git_prefix_window(database, object()),
            ):
                with pytest.raises(KernelError) as caught:
                    consume()
                assert caught.value.code == "git_prefix_sql_control_required"
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                with pytest.raises(KernelError):
                    require_git_prefix_transaction_epoch(database, caller)
                with pytest.raises(KernelError):
                    _require_git_prefix_caller_transaction_epoch(
                        database, git_prefix_transaction_epoch(database)
                    )


@pytest.mark.parametrize("outer_scope", [_git_prefix_transaction_scope, git_prefix_sql_window])
def test_long_scope_conflicts_with_existing_long_or_short_scope(database_path, outer_scope):
    kwargs = {"checkpoint": lambda: None} if outer_scope is git_prefix_sql_window else {}
    with open_prepared_git_connection(database_path, read_only=False) as database:
        with outer_scope(database, **kwargs):
            with pytest.raises(KernelError) as caught:
                with _git_prefix_transaction_scope(database):
                    pytest.fail("长事务观察不能嵌套或覆盖活动短窗口")
            assert caught.value.code == "git_prefix_sql_control_conflict"


def test_nested_short_window_preserves_checkpoint_before_conflict(database_path):
    marker = ValueError("nested checkpoint")

    def reject():
        raise marker

    with open_prepared_git_connection(database_path, read_only=False) as database:
        with _git_prefix_transaction_scope(database):
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                with pytest.raises(ValueError) as caught:
                    with git_prefix_sql_window(database, checkpoint=reject):
                        pytest.fail("嵌套检查点失败不得被冲突错误替换")
                assert caught.value is marker
                checks = []
                with pytest.raises(KernelError) as caught:
                    with git_prefix_sql_window(database, checkpoint=lambda: checks.append(None)):
                        pytest.fail("短窗口不能嵌套")
                assert caught.value.code == "git_prefix_sql_control_conflict"
                assert checks == [None]
                require_git_prefix_sql_window(database)


@pytest.mark.parametrize("short_window", [False, True])
def test_closed_connection_cleanup_preserves_first_error(database_path, short_window):
    marker = ValueError("original body failure")
    with open_prepared_git_connection(database_path, read_only=False) as database:
        with pytest.raises(ValueError) as caught:
            with _git_prefix_transaction_scope(database):
                if short_window:
                    with git_prefix_sql_window(database, checkpoint=lambda: None):
                        database.close()
                        raise marker
                database.close()
                raise marker
        assert caught.value is marker
        assert database not in sql._owned.transaction_traces
        assert database not in getattr(sql._owned, "connections", {})


@pytest.mark.parametrize("scope", [_git_prefix_transaction_scope, git_prefix_sql_window])
def test_partial_installation_cleans_real_callbacks(database_path, monkeypatch, scope):
    marker = ValueError("trace construction failure")
    checks = []
    kwargs = {"checkpoint": lambda: checks.append(None)} if scope is git_prefix_sql_window else {}

    def reject(*_args):
        raise marker

    with open_prepared_git_connection(database_path, read_only=False) as database:
        # 只注入回调构造失败；连接、来源登记和进度回调均使用真实实现。
        with monkeypatch.context() as patch:
            patch.setattr(sql, "partial", reject)
            with pytest.raises(ValueError) as caught:
                with scope(database, **kwargs):
                    pytest.fail("安装失败不得进入窗口")
        assert caught.value is marker
        assert database not in getattr(sql._owned, "transaction_traces", {})
        assert database not in getattr(sql._owned, "connections", {})
        count = len(checks)
        database.execute(_BUSY_SQL).fetchone()
        assert len(checks) == count
        with scope(database, **kwargs):
            database.execute("BEGIN").close()


@pytest.mark.parametrize("callback", [False, True])
async def test_foreign_task_or_callback_cannot_use_original_scope(database_path, callback):
    with open_prepared_git_connection(database_path, read_only=False) as database:
        with _git_prefix_transaction_scope(database):
            database.execute("BEGIN").close()
            epoch = _git_prefix_caller_transaction_epoch(database)

            def borrow():
                for read in (
                    lambda: _git_prefix_caller_transaction_epoch(database),
                    lambda: _require_git_prefix_caller_transaction_epoch(database, epoch),
                ):
                    with pytest.raises(KernelError) as caught:
                        read()
                    assert caught.value.code == "git_prefix_sql_owner_invalid"
                with pytest.raises(KernelError) as caught:
                    with _git_prefix_transaction_scope(database):
                        pytest.fail("其他Task不能安装原连接观察")
                assert caught.value.code == "git_prepared_link_host_invalid"

            if callback:
                result = asyncio.get_running_loop().create_future()

                def callback_borrow():
                    try:
                        borrow()
                    except BaseException as error:
                        result.set_exception(error)
                    else:
                        result.set_result(None)

                asyncio.get_running_loop().call_soon(callback_borrow)
                await result
            else:

                async def task_borrow():
                    borrow()

                await asyncio.create_task(task_borrow())
            _require_git_prefix_caller_transaction_epoch(database, epoch)


def test_revoked_real_factory_source_invalidates_scope(database_path):
    lifetime = open_prepared_git_connection(database_path, read_only=False)
    database = lifetime.__enter__()
    try:
        with _git_prefix_transaction_scope(database):
            database.execute("BEGIN").close()
            epoch = _git_prefix_caller_transaction_epoch(database)
            lifetime.__exit__(None, None, None)
            for read in (
                lambda: _git_prefix_caller_transaction_epoch(database),
                lambda: _require_git_prefix_caller_transaction_epoch(database, epoch),
            ):
                with pytest.raises(KernelError) as caught:
                    read()
                assert caught.value.code == "git_prefix_sql_owner_invalid"
    finally:
        lifetime.__exit__(None, None, None)


def test_scope_identity_invalidates_old_token_even_without_sql_boundaries(database_path):
    with open_prepared_git_connection(database_path, read_only=False) as database:
        database.execute("BEGIN").close()
        with _git_prefix_transaction_scope(database):
            old = _git_prefix_caller_transaction_epoch(database)
        with pytest.raises(KernelError):
            _require_git_prefix_caller_transaction_epoch(database, old)
        with _git_prefix_transaction_scope(database):
            current = _git_prefix_caller_transaction_epoch(database)
            assert current[0] is not old[0]
            assert current[1] == old[1] == 0
            with pytest.raises(KernelError):
                _require_git_prefix_caller_transaction_epoch(database, old)
            _require_git_prefix_caller_transaction_epoch(database, current)
            with open_prepared_git_connection(database_path, read_only=False) as other:
                other.execute("BEGIN").close()
                with _git_prefix_transaction_scope(other):
                    with pytest.raises(KernelError) as caught:
                        _require_git_prefix_caller_transaction_epoch(other, current)
                    assert caught.value.code == "publication_history_unproven"


def test_generic_window_without_long_scope_restores_none(database_path, monkeypatch):
    traces = []
    checks = []
    dispatch = sql._dispatch_git_prefix_trace

    def observe(database, statement):
        traces.append(None)
        dispatch(database, statement)

    monkeypatch.setattr(sql, "_dispatch_git_prefix_trace", observe)
    with closing(sqlite3.connect(database_path, isolation_level=None)) as database:
        database.execute("BEGIN").close()
        with git_prefix_sql_window(database, checkpoint=lambda: checks.append(None)):
            short = git_prefix_transaction_epoch(database)
            database.execute("SAVEPOINT generic").close()
            database.execute("RELEASE generic").close()
            assert git_prefix_transaction_epoch(database) == (short[0], short[1] + 2)
        count = len(traces), len(checks)
        database.execute(_BUSY_SQL).fetchone()
        database.rollback()
        assert (len(traces), len(checks)) == count
