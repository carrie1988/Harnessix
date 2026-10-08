"""SQL消费窗口仅属原Task；源观察回调不能借出前缀发布权限。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_prefix_sql import (
    git_prefix_sql_window,
    git_prefix_transaction_epoch,
    require_git_prefix_sql_window,
)
from harnessix.product_config.git_prepared_link_connection import open_prepared_git_connection


@pytest.mark.parametrize("callback", [False, True])
async def test_foreign_task_cannot_register_sql_window_on_prepared_connection(tmp_path, callback):
    path = tmp_path / "original.db"
    with closing(sqlite3.connect(path)) as seed:
        seed.execute("CREATE TABLE source (value TEXT)")
    with open_prepared_git_connection(path, read_only=False) as database:

        def borrow():
            with pytest.raises(KernelError) as caught:
                with git_prefix_sql_window(database, checkpoint=lambda: None):
                    pytest.fail("其他Task不得接管产品原连接的SQL窗口")
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
        with git_prefix_sql_window(database, checkpoint=lambda: None):
            require_git_prefix_sql_window(database)


def test_sync_prepared_connection_cannot_be_downgraded_to_generic_async_sql_admission(tmp_path):
    path = tmp_path / "original.db"
    with closing(sqlite3.connect(path)) as seed:
        seed.execute("CREATE TABLE source (value TEXT)")
    with open_prepared_git_connection(path, read_only=False) as database:
        with git_prefix_sql_window(database, checkpoint=lambda: None):

            async def borrow():
                with pytest.raises(KernelError) as caught:
                    require_git_prefix_sql_window(database)
                assert caught.value.code == "git_prepared_link_host_invalid"

            asyncio.run(borrow())
            require_git_prefix_sql_window(database)


def test_generic_unregistered_sync_sql_window_keeps_original_thread_contract():
    with closing(sqlite3.connect(":memory:")) as database:
        with git_prefix_sql_window(database, checkpoint=lambda: None):

            async def consume_generic():
                require_git_prefix_sql_window(database)

            asyncio.run(consume_generic())


async def test_consumer_rechecks_prepared_source_after_checkpoint_revokes_context(tmp_path):
    path = tmp_path / "original.db"
    with closing(sqlite3.connect(path)) as seed:
        seed.execute("CREATE TABLE source (value TEXT)")
    lifetime = open_prepared_git_connection(path, read_only=False)
    database = lifetime.__enter__()
    revoke = False

    def checkpoint():
        nonlocal revoke
        if revoke:
            revoke = False
            lifetime.__exit__(None, None, None)

    try:
        with git_prefix_sql_window(database, checkpoint=checkpoint):
            revoke = True
            with pytest.raises(KernelError) as caught:
                require_git_prefix_sql_window(database)
            assert caught.value.code == "git_prefix_sql_owner_invalid"
    finally:
        lifetime.__exit__(None, None, None)


@pytest.mark.parametrize("entry", [require_git_prefix_sql_window, git_prefix_transaction_epoch])
@pytest.mark.parametrize("callback", [False, True])
async def test_sibling_task_or_loop_callback_cannot_enter_original_sql_window(entry, callback):
    checks = []
    with closing(sqlite3.connect(":memory:")) as database:
        with git_prefix_sql_window(database, checkpoint=lambda: checks.append(None)):
            count = len(checks)

            def borrow():
                with pytest.raises(KernelError) as caught:
                    entry(database)
                assert caught.value.code == "git_prefix_sql_owner_invalid"
                assert len(checks) == count

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
            assert require_git_prefix_sql_window(database) is None


async def test_progress_callback_keeps_source_checks_in_original_task():
    checks = []
    with closing(sqlite3.connect(":memory:")) as database:
        with git_prefix_sql_window(database, checkpoint=lambda: checks.append(None)):
            require_git_prefix_sql_window(database)
            before = len(checks)
            database.execute(
                "WITH RECURSIVE numbers(x) AS (VALUES(0) UNION ALL "
                "SELECT x+1 FROM numbers WHERE x<1000) SELECT SUM(x) FROM numbers"
            ).fetchone()
            assert len(checks) > before
            await asyncio.sleep(0)
            require_git_prefix_sql_window(database)


async def test_original_interruption_keeps_first_error_identity_even_for_other_task():
    original = KernelError("original-cancel", "原检查点失败")

    def reject():
        raise original

    with closing(sqlite3.connect(":memory:")) as database:
        with pytest.raises(KernelError) as caught:
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                # 模拟SQL驱动已经捕获的首失败；后续Task不得替换该异常。
                from harnessix.product_config import git_prefix_sql

                control = git_prefix_sql._owned.connections[database]
                control.checkpoint = reject
                assert control.interrupt() == 1

                async def read_failure():
                    require_git_prefix_sql_window(database)

                await asyncio.create_task(read_failure())
        assert caught.value is original
