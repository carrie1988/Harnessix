from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime_thread_lock import RuntimeThreadLock


def assert_unowned(action: Callable[[], object]) -> None:
    with pytest.raises(KernelError) as error:
        action()
    assert error.value.code == "runtime_thread_lock_unowned"
    assert error.value.message == "当前 Task 未持有 Runtime Thread 锁"


def test_observer_cannot_be_issued_without_event_loop() -> None:
    lock = RuntimeThreadLock()
    assert_unowned(lock.observe_owner)
    assert lock._owner is None
    assert lock._owner_generation is None


async def test_observer_cannot_be_issued_without_owner() -> None:
    lock = RuntimeThreadLock()
    assert_unowned(lock.observe_owner)
    assert lock._owner_generation is None


async def test_each_acquisition_has_a_unique_generation_and_permanently_revokes_observers() -> None:
    lock = RuntimeThreadLock()
    generations: list[object] = []
    observers: list[Callable[[], None]] = []
    for _ in range(3):
        assert await lock.acquire() is True
        try:
            assert lock._owner is asyncio.current_task()
            generation = lock._owner_generation
            assert type(generation) is object
            assert all(generation is not previous for previous in generations)
            generations.append(generation)
            for observer in observers:
                assert_unowned(observer)
            # 同次持锁可重复签发，但不会创建新的 acquire 代际。
            first, second = lock.observe_owner(), lock.observe_owner()
            assert first() is None
            assert second() is None
            assert lock._owner_generation is generation
            observers.extend((first, second))
            await asyncio.sleep(0)
            first()
            second()
        finally:
            lock.release()
        assert lock._owner is None
        assert lock._owner_generation is None
        for observer in observers:
            assert_unowned(observer)


@pytest.mark.parametrize("execution", ["create_task", "task_group", "wait_for"])
async def test_managed_child_observes_without_receiving_owner_permissions(execution: str) -> None:
    lock = RuntimeThreadLock()
    async with lock:
        owner, generation = lock._owner, lock._owner_generation
        observer = lock.observe_owner()

        async def child() -> None:
            assert asyncio.current_task() is not owner
            assert observer() is None
            assert_unowned(lock.require_current_owner)
            assert_unowned(lock.release)
            assert_unowned(lock.observe_owner)
            assert lock._owner is owner
            assert lock._owner_generation is generation
            await asyncio.sleep(0)
            assert observer() is None

        if execution == "create_task":
            await asyncio.create_task(child())
        elif execution == "task_group":
            async with asyncio.TaskGroup() as group:
                group.create_task(child())
        else:
            await asyncio.wait_for(asyncio.create_task(child()), 2)
        lock.require_current_owner()
        assert observer() is None
    assert_unowned(observer)


async def test_wait_for_coroutine_checks_actual_task_not_wrapper_identity() -> None:
    lock = RuntimeThreadLock()
    async with lock:
        owner = lock._owner
        observer = lock.observe_owner()

        async def probe() -> None:
            observer()
            # wait_for 在不同 Python 实现中可能直接执行协程；权限以实际 Task 为准。
            if asyncio.current_task() is owner:
                lock.require_current_owner()
                assert lock.observe_owner()() is None
            else:
                assert_unowned(lock.require_current_owner)
                assert_unowned(lock.observe_owner)

        await asyncio.wait_for(probe(), 2)
        observer()
    assert_unowned(observer)


async def test_callback_can_observe_but_cannot_issue_check_owner_or_release() -> None:
    lock = RuntimeThreadLock()
    loop = asyncio.get_running_loop()
    checked: asyncio.Future[None] = loop.create_future()
    async with lock:
        observer = lock.observe_owner()

        def callback() -> None:
            try:
                assert asyncio.current_task() is None
                assert observer() is None
                assert_unowned(lock.observe_owner)
                assert_unowned(lock.require_current_owner)
                assert_unowned(lock.release)
            except BaseException as error:
                checked.set_exception(error)
            else:
                checked.set_result(None)

        loop.call_soon(callback)
        await asyncio.wait_for(checked, 2)
        lock.require_current_owner()
        observer()
    assert_unowned(observer)


async def test_observer_freezes_acquiring_task_not_coroutine_creator() -> None:
    lock = RuntimeThreadLock()
    acquisition = lock.acquire()
    issued: asyncio.Future[Callable[[], None]] = asyncio.get_running_loop().create_future()
    finish = asyncio.Event()

    async def owner() -> None:
        assert await acquisition is True
        try:
            issued.set_result(lock.observe_owner())
            await finish.wait()
        finally:
            lock.release()

    task = asyncio.create_task(owner())
    try:
        observer = await asyncio.wait_for(issued, 2)
        assert lock._owner is task
        assert observer() is None
        assert_unowned(lock.observe_owner)
        assert_unowned(lock.require_current_owner)
        assert_unowned(lock.release)
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
    assert_unowned(observer)


async def test_new_task_owner_does_not_revalidate_old_observer() -> None:
    lock = RuntimeThreadLock()
    async with lock:
        original_owner, original_generation = lock._owner, lock._owner_generation
        old_observer = lock.observe_owner()
    issued: asyncio.Future[Callable[[], None]] = asyncio.get_running_loop().create_future()
    finish = asyncio.Event()

    async def successor() -> None:
        async with lock:
            assert lock._owner is not original_owner
            assert lock._owner_generation is not original_generation
            assert_unowned(old_observer)
            issued.set_result(lock.observe_owner())
            await finish.wait()

    task = asyncio.create_task(successor())
    try:
        new_observer = await asyncio.wait_for(issued, 2)
        assert lock._owner is task
        assert_unowned(old_observer)
        assert new_observer() is None
        assert_unowned(lock.observe_owner)
        assert_unowned(lock.release)
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
    async with lock:
        assert_unowned(old_observer)
        assert_unowned(new_observer)
        assert lock.observe_owner()() is None


async def test_observers_are_bound_to_the_original_lock() -> None:
    first, second = RuntimeThreadLock(), RuntimeThreadLock()
    async with second:
        second_observer = second.observe_owner()
        async with first:
            first_observer = first.observe_owner()
            assert first._owner is second._owner
            assert first._owner_generation is not second._owner_generation
            first_observer()
            second_observer()
        assert_unowned(first_observer)
        assert second_observer() is None
        async with first:
            assert_unowned(first_observer)
            second_observer()
    assert_unowned(second_observer)


@pytest.mark.parametrize("when", ["waiting", "woken"])
async def test_cancelled_waiter_preserves_or_revokes_the_original_observer(when: str) -> None:
    lock = RuntimeThreadLock()
    started, next_started = asyncio.Event(), asyncio.Event()
    acquired: list[str] = []
    await lock.acquire()
    observer, generation = lock.observe_owner(), lock._owner_generation

    async def waiter() -> None:
        started.set()
        async with lock:
            acquired.append("cancelled")

    async def successor() -> None:
        next_started.set()
        async with lock:
            assert_unowned(observer)
            assert lock._owner_generation is not generation
            assert lock.observe_owner()() is None
            acquired.append("successor")

    first = asyncio.create_task(waiter())
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(successor())
        await asyncio.wait_for(next_started.wait(), 2)
        if when == "woken":
            # 释放后同步取消已唤醒的等待者，覆盖其恢复执行前的竞争窗口。
            lock.release()
            assert lock._owner_generation is None
            assert_unowned(observer)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        if when == "waiting":
            assert observer() is None
            assert lock._owner_generation is generation
            lock.release()
            assert_unowned(observer)
        await asyncio.wait_for(second, 2)
    finally:
        if lock._owner is asyncio.current_task():
            lock.release()
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
    assert acquired == ["successor"]
    assert lock._owner_generation is None
    assert_unowned(observer)


async def test_waiter_timeout_does_not_replace_owner_generation() -> None:
    lock = RuntimeThreadLock()
    entered = asyncio.Event()
    deadlines: list[asyncio.Timeout] = []
    async with lock:
        observer, generation = lock.observe_owner(), lock._owner_generation

        async def waiter() -> None:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(None) as deadline:
                    deadlines.append(deadline)
                    entered.set()
                    async with lock:
                        pytest.fail("超时等待者不应获取锁")
            assert_unowned(lock.observe_owner)
            assert observer() is None

        task = asyncio.create_task(waiter())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            await asyncio.wait_for(task, 2)
            assert observer() is None
            assert lock._owner_generation is generation
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert_unowned(observer)


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_owner_context_interruption_revokes_observer(interruption: str) -> None:
    lock = RuntimeThreadLock()
    issued: asyncio.Future[Callable[[], None]] = asyncio.get_running_loop().create_future()
    deadlines: list[asyncio.Timeout] = []

    async def owner() -> None:
        async with asyncio.timeout(None) as deadline:
            deadlines.append(deadline)
            async with lock:
                issued.set_result(lock.observe_owner())
                await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    try:
        observer = await asyncio.wait_for(issued, 2)
        assert observer() is None
        if interruption == "cancel":
            task.cancel()
            expected = asyncio.CancelledError
        else:
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            expected = TimeoutError
        # 请求取消或超时本身不提前剥夺锁；实际退出持锁上下文才撤销观察函数。
        assert observer() is None
        with pytest.raises(expected):
            await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not lock.locked()
    assert lock._owner is None
    assert lock._owner_generation is None
    assert_unowned(observer)
    async with lock:
        assert_unowned(observer)
        assert lock.observe_owner()() is None


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_child_interruption_does_not_release_original_owner(interruption: str) -> None:
    lock = RuntimeThreadLock()
    entered = asyncio.Event()
    deadlines: list[asyncio.Timeout] = []
    async with lock:
        observer, owner, generation = lock.observe_owner(), lock._owner, lock._owner_generation

        async def child() -> None:
            try:
                async with asyncio.timeout(None) as deadline:
                    deadlines.append(deadline)
                    observer()
                    entered.set()
                    await asyncio.Event().wait()
            finally:
                observer()
                assert_unowned(lock.require_current_owner)
                assert_unowned(lock.release)
                assert_unowned(lock.observe_owner)

        task = asyncio.create_task(child())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if interruption == "cancel":
                task.cancel()
                expected = asyncio.CancelledError
            else:
                deadlines[0].reschedule(asyncio.get_running_loop().time())
                expected = TimeoutError
            with pytest.raises(expected):
                await asyncio.wait_for(task, 2)
            assert observer() is None
            assert lock._owner is owner
            assert lock._owner_generation is generation
            lock.require_current_owner()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert_unowned(observer)


async def test_failed_reentrant_acquisition_preserves_observer() -> None:
    lock = RuntimeThreadLock()
    async with lock:
        observer, generation = lock.observe_owner(), lock._owner_generation
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await lock.acquire()
        assert observer() is None
        assert lock._owner_generation is generation
        lock.require_current_owner()
    assert_unowned(observer)


@pytest.mark.parametrize(
    "original",
    [
        RuntimeError("原异常"),
        KernelError("test_original", "原异常"),
        asyncio.CancelledError("原取消"),
    ],
)
async def test_observed_child_failure_preserves_original_exception_instance(
    original: BaseException,
) -> None:
    lock = RuntimeThreadLock()
    with pytest.raises(type(original)) as caught:
        async with lock:
            observer = lock.observe_owner()

            async def child() -> None:
                observer()
                raise original

            await asyncio.create_task(child())
    assert caught.value is original
    assert not lock.locked()
    assert lock._owner is None
    assert lock._owner_generation is None
    assert_unowned(observer)


async def test_successful_acquire_registers_generation_without_a_new_suspension() -> None:
    lock = RuntimeThreadLock()

    async def owner() -> None:
        task = asyncio.current_task()
        assert task is not None
        # 若成功 acquire 后新增挂起点，排队的取消会阻止下面签发观察函数。
        asyncio.get_running_loop().call_soon(task.cancel)
        assert await lock.acquire() is True
        try:
            assert lock._owner is task
            assert type(lock._owner_generation) is object
            observer = lock.observe_owner()
            assert observer() is None
            with pytest.raises(asyncio.CancelledError):
                await asyncio.sleep(0)
            assert observer() is None
        finally:
            lock.release()
        assert_unowned(observer)

    await asyncio.create_task(owner())
    assert not lock.locked()
    assert lock._owner_generation is None


async def test_issuer_check_cannot_be_bypassed_by_overriding_current_owner_check() -> None:
    class UncheckedLock(RuntimeThreadLock):
        def require_current_owner(self) -> None:
            pass

    lock = UncheckedLock()
    async with lock:
        observer = lock.observe_owner()

        async def child() -> None:
            assert_unowned(lock.observe_owner)
            assert observer() is None

        await asyncio.create_task(child())
        assert observer() is None
    assert_unowned(observer)
