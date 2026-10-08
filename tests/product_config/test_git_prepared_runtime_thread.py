"""实际 SDK 原 Runtime、连接及持锁代际的 Git 消费边界；不证明 Git 写效果。"""

from __future__ import annotations

import asyncio
import copy
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.runtime_thread_lock import RuntimeThreadLock
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prepared_link_connection import open_prepared_git_connection
from harnessix.product_config.git_prepared_runtime_thread import (
    _prepared_runtime_thread_observer,
    bind_prepared_git_runtime_thread,
    require_prepared_git_runtime_thread,
)
from tests.product_config.test_git_prepared_approval_history import _decide, _reader
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _ledger,
    _rows,
)


async def test_actual_scope_lives_through_commit_and_child_is_only_an_observer(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        thread_id = actual.thread.thread_id
        async with _database(actual) as database:
            runtime._require_thread_lock(thread_id)
            observe = _prepared_runtime_thread_observer(
                database, actual.scenario.router, actual.artifacts
            )
            await _genesis(actual, database)
            database.execute("BEGIN IMMEDIATE")
            result = await _ledger(actual, database).prepare(
                actual.route.plan.execution.plan_id,
                cancel=CancelToken(),
                checkpoint=lambda: None,
            )
            assert result.plan.core.thread_id == thread_id
            runtime._require_thread_lock(thread_id)

            async def child():
                observe()
                with pytest.raises(KernelError) as caught:
                    require_prepared_git_runtime_thread(database, thread_id)
                assert caught.value.code == "git_runtime_thread_scope_invalid"
                with pytest.raises(KernelError) as caught:
                    _prepared_runtime_thread_observer(
                        database, actual.scenario.router, actual.artifacts
                    )
                assert caught.value.code == "git_runtime_thread_scope_invalid"
                with pytest.raises(KernelError) as caught:
                    with git_prefix_sql_window(database, checkpoint=lambda: None):
                        pytest.fail("观察不能授予原连接 SQL 窗口")
                assert caught.value.code == "git_prepared_link_host_invalid"
                with pytest.raises(KernelError) as caught:
                    runtime._require_thread_lock(thread_id)
                assert caught.value.code == "runtime_thread_lock_unowned"

            await CancelToken().run(child(), preserve_failure=True)
            observe()
            database.execute("COMMIT")
            runtime._require_thread_lock(thread_id)
            assert not database.in_transaction
            committed = _rows(database)
        assert not runtime._lock(thread_id).locked()
        with pytest.raises(KernelError) as caught:
            observe()
        assert caught.value.code == "git_runtime_thread_scope_invalid"
        async with _database(actual, read_only=True) as database:
            database.execute("BEGIN")
            assert await _ledger(actual, database).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            ) == (result,)
            assert _rows(database) == committed
            assert database.total_changes == 0

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("operation", ["prepare", "read", "history"])
async def test_actual_connection_without_runtime_scope_is_not_product_authority(
    tmp_path, config, monkeypatch, operation
):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        async with _database(actual) as database:
            await _genesis(actual, database)
            baseline = _rows(database)
        path = actual.scenario.state / "git-delivery" / "git-delivery.db"
        async with runtime._lock(actual.thread.thread_id):
            with open_prepared_git_connection(path, read_only=operation != "prepare") as database:
                database.execute("BEGIN IMMEDIATE" if operation == "prepare" else "BEGIN")
                with pytest.raises(KernelError) as caught:
                    if operation == "prepare":
                        await _ledger(actual, database).prepare(
                            actual.route.plan.execution.plan_id,
                            cancel=CancelToken(),
                            checkpoint=lambda: None,
                        )
                    elif operation == "read":
                        await _ledger(actual, database).read_all(
                            cancel=CancelToken(), checkpoint=lambda: None
                        )
                    else:
                        await _reader(actual, database).read_all(
                            cancel=CancelToken(), checkpoint=lambda: None
                        )
                assert caught.value.code == "git_runtime_thread_scope_invalid"
                assert _rows(database) == baseline
                assert database.in_transaction and database.total_changes == 0
                database.execute("ROLLBACK")

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_real_held_lock_for_another_thread_cannot_publish_target_link(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        async with _database(actual) as database:
            await _genesis(actual, database)
            baseline = _rows(database)
        path = actual.scenario.state / "git-delivery" / "git-delivery.db"
        other_thread = uuid4()
        async with runtime._lock(other_thread):
            with (
                open_prepared_git_connection(path, read_only=False) as database,
                bind_prepared_git_runtime_thread(database, runtime, other_thread),
            ):
                database.execute("BEGIN IMMEDIATE")
                with pytest.raises(KernelError) as caught:
                    await _ledger(actual, database).prepare(
                        actual.route.plan.execution.plan_id,
                        cancel=CancelToken(),
                        checkpoint=lambda: None,
                    )
                assert caught.value.code == "git_runtime_thread_scope_invalid"
                assert _rows(database) == baseline
                assert database.total_changes == 0
                database.execute("ROLLBACK")

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_same_task_release_reacquire_cannot_restore_an_issued_scope(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        thread_id = actual.thread.thread_id
        async with _database(actual) as database:
            await _genesis(actual, database)
            baseline = _rows(database)
        path = actual.scenario.state / "git-delivery" / "git-delivery.db"
        lock = runtime._lock(thread_id)
        async with lock:
            with open_prepared_git_connection(path, read_only=True) as database:
                with pytest.raises(KernelError) as caught:
                    with bind_prepared_git_runtime_thread(database, runtime, thread_id):
                        observe = _prepared_runtime_thread_observer(
                            database, actual.scenario.router, actual.artifacts
                        )
                        lock.release()
                        await lock.acquire()
                        runtime._require_thread_lock(thread_id)
                        observe()
                assert caught.value.code == "runtime_thread_lock_unowned"
                # 原 Thread 当前确已重新持锁，但旧 acquire 来源永久失效。
                with pytest.raises(KernelError):
                    observe()
                assert _rows(database) == baseline
                with bind_prepared_git_runtime_thread(database, runtime, thread_id):
                    require_prepared_git_runtime_thread(database, thread_id)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_actual_runtime_replacements_cannot_borrow_the_old_scope(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        async with _database(actual) as database:
            await _genesis(actual, database)
            observe = _prepared_runtime_thread_observer(
                database, actual.scenario.router, actual.artifacts
            )
            targets = (
                (runtime, "_trusted_actions", copy.copy(runtime._trusted_actions)),
                (runtime, "_artifacts", copy.copy(runtime._artifacts)),
                (runtime, "store", copy.copy(runtime.store)),
                (runtime, "_owner", copy.copy(runtime._owner)),
                (runtime._trusted_actions, "_state", copy.copy(runtime._trusted_actions._state)),
            )
            for target, name, replacement in targets:
                with monkeypatch.context() as patch:
                    patch.setattr(target, name, replacement)
                    with pytest.raises(KernelError) as caught:
                        observe()
                    assert caught.value.code == "git_runtime_thread_scope_invalid"
                observe()
            with monkeypatch.context() as patch:
                patch.setitem(runtime._locks, actual.thread.thread_id, RuntimeThreadLock())
                with pytest.raises(KernelError) as caught:
                    observe()
                assert caught.value.code == "git_runtime_thread_scope_invalid"
            observe()

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_sdk_approval_waits_for_original_database_window_then_completes(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        runtime = actual.scenario.client.transport.server.service.runtime
        task = None
        try:
            async with _database(actual) as database:
                await _genesis(actual, database)
                database.execute("BEGIN IMMEDIATE")
                await _ledger(actual, database).prepare(
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
                task = asyncio.create_task(_decide(actual, monkeypatch, "approved"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not task.done()
                runtime._require_thread_lock(actual.thread.thread_id)
                assert (
                    actual.scenario.router.status(actual.route.plan.execution.plan_id).state
                    == "pending_approval"
                )
                database.execute("COMMIT")
            await asyncio.wait_for(task, 10)
            async with _database(actual, read_only=True) as database:
                database.execute("BEGIN")
                histories = await _reader(actual, database).read_all(
                    cancel=CancelToken(), checkpoint=lambda: None
                )
                assert len(histories) == 1
                assert histories[0].approval_history.state == "approved"
                assert database.total_changes == 0
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    await _case(tmp_path, config, monkeypatch, inspect)
