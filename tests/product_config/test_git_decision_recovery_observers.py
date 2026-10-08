"""恢复观察的窄负控：只读代际观察绝不授予跨 Task SQL 准入。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prefix_sql as prefix
from harnessix.product_config import git_prepared_link_connection as connection


def _prepare_database(path):
    with closing(sqlite3.connect(path)) as database:
        database.execute("CREATE TABLE records(value TEXT)")
    return path


async def _observe_in_child_task(observe):
    observe()


async def test_parent_issued_observation_allows_child_but_not_sql_or_issuance(tmp_path):
    path = _prepare_database(tmp_path / "facts.db")
    with connection.open_prepared_git_connection(path, read_only=False) as database:
        with prefix._git_prefix_transaction_scope(database):
            database.execute("BEGIN")
            observe = prefix._git_prefix_caller_transaction_observer(database)
            epoch = prefix._git_prefix_caller_transaction_epoch(database)

            async def child():
                observe()
                with pytest.raises(KernelError):
                    prefix._git_prefix_caller_transaction_observer(database)
                with pytest.raises(KernelError):
                    with prefix.git_prefix_sql_window(database, checkpoint=lambda: None):
                        pytest.fail("子 Task 不应获准进入 SQL 窗口")

            await asyncio.create_task(child())
            assert prefix._git_prefix_caller_transaction_epoch(database) == epoch
            assert database.total_changes == 0
            database.execute("ROLLBACK")
            with pytest.raises(KernelError):
                observe()
    with pytest.raises(KernelError):
        observe()


@pytest.mark.parametrize(
    "fault", ["rollback_begin", "savepoint", "trace_replace", "connection_close"]
)
async def test_changed_generation_or_source_never_resurrects_observer(tmp_path, fault):
    path = _prepare_database(tmp_path / "facts.db")
    with connection.open_prepared_git_connection(path, read_only=False) as database:
        with prefix._git_prefix_transaction_scope(database):
            database.execute("BEGIN")
            observe = prefix._git_prefix_caller_transaction_observer(database)
            original = prefix._owned.transaction_traces[database]
            if fault == "rollback_begin":
                database.execute("ROLLBACK")
                database.execute("BEGIN")
            elif fault == "savepoint":
                database.execute("SAVEPOINT another")
            elif fault == "trace_replace":
                replacement = prefix._TransactionTrace(original.prepared_source)
                replacement.epoch = original.epoch
                prefix._owned.transaction_traces[database] = replacement
            else:
                database.close()
            try:
                with pytest.raises(KernelError):
                    await asyncio.create_task(_observe_in_child_task(observe))
            finally:
                prefix._owned.transaction_traces[database] = original
                if fault != "connection_close":
                    database.execute("ROLLBACK")


async def test_registration_observer_is_io_free_and_retires(tmp_path, monkeypatch):
    path = _prepare_database(tmp_path / "facts.db")
    with connection.open_prepared_git_connection(path, read_only=False) as database:
        observe = connection._prepared_git_connection_registration_observer(database)
        statements = []
        database.set_trace_callback(statements.append)

        def forbidden(*args, **kwargs):
            pytest.fail("本地观察不应执行物理路径或 SQL 认证")

        with monkeypatch.context() as injected:
            injected.setattr(connection, "_physical_pin", forbidden)
            injected.setattr(connection, "_database_path", forbidden)
            await asyncio.create_task(_observe_in_child_task(observe))
        assert not statements and database.total_changes == 0
    with pytest.raises(KernelError):
        observe()
