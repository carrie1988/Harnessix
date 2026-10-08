from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ApprovalRequestContent,
    EventDraft,
    ItemFinished,
    Thread,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.runtime_thread_lock import RuntimeThreadLock
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step

THREAD_ID = UUID(int=1)
OTHER_THREAD_ID = UUID(int=2)


@pytest.fixture
def runtime(tmp_path: Path) -> AgentRuntime:
    return AgentRuntime(SQLiteSessionStore(tmp_path / "session.db"), FakeProvider())


def assert_not_owned(runtime: AgentRuntime, thread_id: UUID = THREAD_ID) -> None:
    with pytest.raises(KernelError) as error:
        runtime._require_thread_lock(thread_id)
    assert error.value.code == "runtime_thread_lock_unowned"
    assert error.value.message == "当前 Task 未持有 Runtime Thread 锁"


def assert_release_denied(lock: asyncio.Lock) -> None:
    with pytest.raises(KernelError) as error:
        lock.release()
    assert error.value.code == "runtime_thread_lock_unowned"
    assert error.value.message == "当前 Task 未持有 Runtime Thread 锁"


def test_missing_lock_check_does_not_register_lock(runtime: AgentRuntime) -> None:
    assert_not_owned(runtime)
    assert runtime._locks == {}


def test_registered_unlocked_lock_without_event_loop_is_rejected(runtime: AgentRuntime) -> None:
    lock = runtime._lock(THREAD_ID)
    assert_not_owned(runtime)
    assert_release_denied(lock)
    assert not lock.locked()


async def test_same_task_acquire_release_and_context_manager(runtime: AgentRuntime) -> None:
    lock = runtime._lock(THREAD_ID)
    assert type(lock) is RuntimeThreadLock
    assert isinstance(lock, asyncio.Lock)
    assert runtime._locks[THREAD_ID] is lock
    assert runtime._lock(THREAD_ID) is lock
    assert_not_owned(runtime)

    assert await lock.acquire() is True
    assert lock._owner is asyncio.current_task()
    lock.require_current_owner()
    runtime._require_thread_lock(THREAD_ID)
    await asyncio.sleep(0)
    runtime._require_thread_lock(THREAD_ID)
    lock.release()
    assert lock._owner is None
    assert not lock.locked()
    assert_not_owned(runtime)
    assert_release_denied(lock)

    async with lock as value:
        assert value is None
        runtime._require_thread_lock(THREAD_ID)
    assert not lock.locked()
    assert_not_owned(runtime)


async def test_another_task_cannot_check_or_release_owned_lock(runtime: AgentRuntime) -> None:
    lock = runtime._lock(THREAD_ID)

    async def intruder() -> None:
        assert lock.locked()
        assert_not_owned(runtime)
        assert_release_denied(lock)
        assert lock.locked()

    async with lock:
        await asyncio.create_task(intruder())
        runtime._require_thread_lock(THREAD_ID)
    assert_not_owned(runtime)


async def test_owner_is_acquiring_task_not_coroutine_creator(runtime: AgentRuntime) -> None:
    lock = runtime._lock(THREAD_ID)
    acquisition = lock.acquire()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def owner() -> None:
        assert await acquisition is True
        try:
            runtime._require_thread_lock(THREAD_ID)
            entered.set()
            await finish.wait()
            runtime._require_thread_lock(THREAD_ID)
        finally:
            lock.release()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert lock.locked()
        assert_not_owned(runtime)
        assert_release_denied(lock)
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
    assert not lock.locked()
    assert_not_owned(runtime)


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("lock_type", [asyncio.Lock, RuntimeThreadLock])
async def test_external_lock_is_not_runtime_ownership(
    runtime: AgentRuntime, registered: bool, lock_type: type[asyncio.Lock]
) -> None:
    if registered:
        runtime._lock(THREAD_ID)
    before = dict(runtime._locks)
    async with lock_type():
        assert_not_owned(runtime)
        assert runtime._locks == before


async def test_same_thread_id_in_other_runtime_is_independent(
    runtime: AgentRuntime, tmp_path: Path
) -> None:
    other = AgentRuntime(SQLiteSessionStore(tmp_path / "other.db"), FakeProvider())
    lock = runtime._lock(THREAD_ID)
    other_lock = other._lock(THREAD_ID)
    assert other_lock is not lock
    async with lock:
        runtime._require_thread_lock(THREAD_ID)
        assert_not_owned(other)
        async with other_lock:
            runtime._require_thread_lock(THREAD_ID)
            other._require_thread_lock(THREAD_ID)
        assert_not_owned(other)


@pytest.mark.parametrize("subclass", [False, True])
async def test_replaced_registered_lock_cannot_downgrade_owner_check(
    runtime: AgentRuntime, subclass: bool
) -> None:
    class UncheckedLock(RuntimeThreadLock):
        def require_current_owner(self) -> None:
            pass

    runtime._lock(THREAD_ID)
    replacement = UncheckedLock() if subclass else asyncio.Lock()
    # 模拟私有字典被替换；不能因外来锁的 locked=True 或宽松子类而放行。
    runtime._locks[THREAD_ID] = cast(RuntimeThreadLock, replacement)
    async with replacement:
        assert replacement.locked()
        assert_not_owned(runtime)
        assert runtime._locks[THREAD_ID] is replacement


@pytest.mark.parametrize("when", ["waiting", "woken"])
async def test_waiter_cancellation_preserves_owner_and_wakes_successor(
    runtime: AgentRuntime, when: str
) -> None:
    lock = runtime._lock(THREAD_ID)
    started, next_started = asyncio.Event(), asyncio.Event()
    acquired: list[str] = []

    async def waiter(name: str, ready: asyncio.Event) -> None:
        ready.set()
        try:
            async with lock:
                runtime._require_thread_lock(THREAD_ID)
                acquired.append(name)
        except asyncio.CancelledError:
            assert_not_owned(runtime)
            raise

    await lock.acquire()
    first = asyncio.create_task(waiter("cancelled", started))
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(waiter("successor", next_started))
        await asyncio.wait_for(next_started.wait(), 2)
        runtime._require_thread_lock(THREAD_ID)
        if when == "woken":
            # 唤醒后、等待者恢复执行前取消，验证竞争窗口不登记错误 owner。
            lock.release()
            assert_not_owned(runtime)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        if when == "waiting":
            assert lock.locked()
            runtime._require_thread_lock(THREAD_ID)
            lock.release()
        await asyncio.wait_for(second, 2)
    finally:
        if when == "waiting" and lock.locked():
            lock.release()
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
    assert acquired == ["successor"]
    assert not lock.locked()
    assert_not_owned(runtime)
    async with lock:
        runtime._require_thread_lock(THREAD_ID)


@pytest.mark.parametrize("holding", [False, True])
async def test_timeout_does_not_leave_or_replace_owner(
    runtime: AgentRuntime, holding: bool
) -> None:
    lock = runtime._lock(THREAD_ID)
    entered = asyncio.Event()
    deadlines: list[asyncio.Timeout] = []
    if not holding:
        await lock.acquire()

    async def expire() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(None) as deadline:
                deadlines.append(deadline)
                if not holding:
                    entered.set()
                async with lock:
                    runtime._require_thread_lock(THREAD_ID)
                    entered.set()
                    await asyncio.Event().wait()
        assert_not_owned(runtime)

    task = asyncio.create_task(expire())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert lock.locked()
        if holding:
            assert_not_owned(runtime)
        else:
            runtime._require_thread_lock(THREAD_ID)
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.wait_for(task, 2)
        if not holding:
            runtime._require_thread_lock(THREAD_ID)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not holding:
            lock.release()
    assert not lock.locked()
    async with lock:
        runtime._require_thread_lock(THREAD_ID)


async def test_lock_is_non_reentrant_and_original_owner_can_still_release(
    runtime: AgentRuntime,
) -> None:
    lock = runtime._lock(THREAD_ID)
    async with lock:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await lock.acquire()
        runtime._require_thread_lock(THREAD_ID)
    assert not lock.locked()
    assert_release_denied(lock)
    async with lock:
        runtime._require_thread_lock(THREAD_ID)


async def test_cancellation_inside_context_releases_in_owner_task(runtime: AgentRuntime) -> None:
    lock = runtime._lock(THREAD_ID)
    entered = asyncio.Event()

    async def owner() -> None:
        try:
            async with lock:
                runtime._require_thread_lock(THREAD_ID)
                entered.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert_not_owned(runtime)
            raise

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lock.locked()
    async with lock:
        runtime._require_thread_lock(THREAD_ID)


@pytest.mark.parametrize(
    "original",
    [
        RuntimeError("原异常"),
        KernelError("test_original", "原异常"),
        asyncio.CancelledError("原取消"),
    ],
)
async def test_lock_exit_preserves_original_exception_instance(
    runtime: AgentRuntime, original: BaseException
) -> None:
    lock = runtime._lock(THREAD_ID)
    with pytest.raises(type(original)) as caught:
        async with lock:
            runtime._require_thread_lock(THREAD_ID)
            raise original
    assert caught.value is original
    assert not lock.locked()
    assert runtime._locks[THREAD_ID]._owner is None
    assert_not_owned(runtime)


@pytest.mark.parametrize(
    "original",
    [
        RuntimeError("原异常"),
        KernelError("test_original", "原异常"),
        asyncio.CancelledError("原取消"),
    ],
)
async def test_runtime_exit_preserves_original_exception_instance(
    runtime: AgentRuntime, tmp_path: Path, original: BaseException
) -> None:
    with pytest.raises(type(original)) as caught:
        async with runtime:
            thread = await runtime.create_thread(str(tmp_path))
            assert await runtime.resume_thread(thread.thread_id) == thread
            async with runtime._lock(thread.thread_id):
                runtime._require_thread_lock(thread.thread_id)
                raise original
    assert caught.value is original
    assert runtime._owner is None
    assert not runtime._open
    assert not runtime._lock(thread.thread_id).locked()
    assert runtime._locks[thread.thread_id]._owner is None
    assert_not_owned(runtime, thread.thread_id)
    async with AgentRuntime(runtime.store, FakeProvider()) as reopened:
        assert await reopened.resume_thread(thread.thread_id) == thread


async def test_callback_without_task_cannot_check_release_or_acquire(
    runtime: AgentRuntime,
) -> None:
    lock = runtime._lock(THREAD_ID)
    loop = asyncio.get_running_loop()
    checked: asyncio.Future[None] = loop.create_future()

    def callback() -> None:
        try:
            assert asyncio.current_task() is None
            assert_not_owned(runtime)
            assert_release_denied(lock)
            acquisition = runtime._lock(OTHER_THREAD_ID).acquire()
            try:
                with pytest.raises(KernelError) as error:
                    acquisition.send(None)
                assert error.value.code == "runtime_thread_lock_unowned"
            finally:
                acquisition.close()
            assert not runtime._lock(OTHER_THREAD_ID).locked()
        except BaseException as error:
            checked.set_exception(error)
        else:
            checked.set_result(None)

    async with lock:
        loop.call_soon(callback)
        await asyncio.wait_for(checked, 2)
        runtime._require_thread_lock(THREAD_ID)


async def test_multiple_threads_have_independent_task_owners(runtime: AgentRuntime) -> None:
    first, second = runtime._lock(THREAD_ID), runtime._lock(OTHER_THREAD_ID)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def other_owner() -> None:
        async with second:
            runtime._require_thread_lock(OTHER_THREAD_ID)
            assert_not_owned(runtime, THREAD_ID)
            entered.set()
            await finish.wait()

    async with first:
        task = asyncio.create_task(other_owner())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            runtime._require_thread_lock(THREAD_ID)
            assert second.locked()
            assert_not_owned(runtime, OTHER_THREAD_ID)
        finally:
            finish.set()
            await asyncio.wait_for(task, 2)
        async with second:
            runtime._require_thread_lock(THREAD_ID)
            runtime._require_thread_lock(OTHER_THREAD_ID)
    assert_not_owned(runtime, THREAD_ID)
    assert_not_owned(runtime, OTHER_THREAD_ID)


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_runtime_close_waits_for_original_approval_owner(
    tmp_path: Path, cancel_close: bool
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    runtime: AgentRuntime

    class CheckingStore(SQLiteSessionStore):
        async def append(
            self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
        ) -> Thread:
            if any(
                isinstance(draft.payload, ItemFinished)
                and isinstance(draft.payload.content, ApprovalRequestContent)
                and draft.payload.content.decision is not None
                for draft in drafts
            ):
                runtime._require_thread_lock(thread_id)
                entered.set()
                await finish.wait()
                runtime._require_thread_lock(thread_id)
            return await super().append(thread_id, drafts, expected_sequence=expected_sequence)

    store = CheckingStore(tmp_path / "session.db")
    tools = RecordingTools(approval=True)
    runtime = AgentRuntime(store, ScriptedProvider([tool_step("test.read")]), tools)
    await runtime.__aenter__()
    reply: asyncio.Task[object] | None = None
    closing: asyncio.Task[None] | None = None
    try:
        thread = await runtime.create_thread(str(tmp_path))
        waiting = await runtime.run_turn(thread.thread_id, "任务", request_id="approval")
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        request = next(
            item.content
            for item in waiting.items
            if isinstance(item.content, ApprovalRequestContent)
        )
        reply = asyncio.create_task(
            runtime.reply_approval(
                thread.thread_id,
                waiting.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="测试用户"),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime._lock(thread.thread_id).locked()
        assert_not_owned(runtime, thread.thread_id)
        assert_release_denied(runtime._lock(thread.thread_id))
        closing = asyncio.create_task(runtime.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(KernelError) as error:
            runtime._ensure_open()
        assert error.value.code == "runtime_closed"
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        finish.set()
        await asyncio.wait_for(reply, 2)
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 2)
        else:
            await asyncio.wait_for(closing, 2)
    finally:
        finish.set()
        await asyncio.gather(*(task for task in (reply, closing) if task), return_exceptions=True)
        if runtime._owner is not None:
            await runtime.__aexit__(None, None, None)

    assert runtime._owner is None
    assert not runtime._lock(thread.thread_id).locked()
    assert_not_owned(runtime, thread.thread_id)
    assert tools.calls == []
    persisted = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == persisted
    # Provider 按持久 model step 索引脚本；重开后续跑的是第二步而非第一步。
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, tools) as reopened:
        completed = await reopened.resume_turn(thread.thread_id, waiting.turn_id)
        assert completed.status is TurnStatus.COMPLETED, completed.error
    assert len(tools.calls) == 1
    assert len(provider.requests) == 1 and provider.requests[0].step == 2
