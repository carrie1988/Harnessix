"""持锁同步宿主原语测试；窄端口不代表实际认证 SDK 或 Git 恢复屏障。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    ApprovalRequestContent,
    Budget,
    EventDraft,
    Item,
    ItemFinished,
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.ports import TrustedActionGateway
from harnessix.agent.reducer import apply_event
from harnessix.agent.runtime_thread_lock import RuntimeThreadLock
from harnessix.agent.trusted_action_runtime import TrustedActionSessionRuntime
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord, EffectClass, ToolDescriptor
from harnessix.session.ports import SessionStore

THREAD_ID, TURN_ID, CALL_ID, ITEM_ID = (UUID(int=value) for value in range(1, 5))
CREATED_AT = datetime(2026, 10, 8, tzinfo=UTC)
DECIDED_AT = CREATED_AT + timedelta(seconds=7)


def waiting_thread() -> Thread:
    call = ToolCallContent(
        call_id=CALL_ID,
        provider_call_id="locked-sync-call",
        tool="workspace.patch",
        tool_version="1",
        effect_class=EffectClass.IDEMPOTENT_WRITE,
        requires_approval=True,
    )
    approval = TrustedActionApprovalRequestContent(
        approval_id=UUID(int=5),
        call_id=CALL_ID,
        presentation="tool",
        plan_id=UUID(int=6),
        plan_fingerprint="a" * 64,
        execution_fingerprint="b" * 64,
        request_fingerprint="c" * 64,
        policy_id="locked-sync-policy",
        policy_version="1",
    )
    turn = Turn(
        turn_id=TURN_ID,
        request_id="locked-sync-turn",
        request_fingerprint="d" * 64,
        status=TurnStatus.WAITING_APPROVAL,
        budget=Budget(),
        created_at=CREATED_AT,
        items=(
            Item(item_id=UUID(int=7), status=ItemStatus.COMPLETED, content=call),
            Item(item_id=ITEM_ID, status=ItemStatus.STARTED, content=approval),
        ),
    )
    return Thread(
        thread_id=THREAD_ID,
        workspace="/locked-sync-fixture",
        sequence=9,
        active_turn_id=TURN_ID,
        turns=(turn,),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


class SyncStore:
    """只实现本原语使用的 Session 读与 CAS append，并复用真实 Reducer。"""

    def __init__(self, probe: SyncProbe) -> None:
        self.probe = probe
        self.thread = waiting_thread()
        self.appends: list[tuple[UUID, tuple[EventDraft, ...], int]] = []

    async def get_thread(self, thread_id: UUID) -> Thread:
        assert thread_id == THREAD_ID
        self.probe.visit("get_thread")
        await self.probe.suspend("get_thread")
        return self.thread

    async def append(
        self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
    ) -> Thread:
        self.probe.visit("append")
        self.appends.append((thread_id, tuple(drafts), expected_sequence))
        await self.probe.suspend("append")
        if expected_sequence != self.thread.sequence:
            raise self.probe.cas_error
        for draft in drafts:
            self.thread = apply_event(
                self.thread,
                AgentEvent(
                    **draft.model_dump(),
                    thread_id=thread_id,
                    sequence=self.thread.sequence + 1,
                ),
            )
        return self.thread


class SyncGateway:
    """只暴露目录和原同步投影；意外 decide/execute/recover 调用直接失败。"""

    def __init__(self, probe: SyncProbe) -> None:
        self.probe = probe
        self.projected: TrustedActionApprovalRequestContent | None = None
        self.received: list[tuple[Thread, Turn, ToolCallContent, object]] = []

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return ()

    def sync_decision(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
    ) -> TrustedActionApprovalRequestContent | None:
        self.probe.visit("gateway")
        self.received.append((thread, turn, call, approval))
        return self.projected


class SyncProbe:
    def __init__(self, lock: asyncio.Lock | None = None) -> None:
        self.lock = lock if lock is not None else RuntimeThreadLock()
        self.locks = {THREAD_ID: self.lock}
        self.trace: list[str] = []
        self.snapshots: list[tuple[object, object, bool]] = []
        self.hooks: dict[str, Callable[[], None]] = {}
        self.async_hooks: dict[str, Callable[[], Awaitable[None]]] = {}
        self.errors: dict[str, BaseException] = {}
        self.lock_lookups = 0
        self.lock_lookup_hook: Callable[[int], None] | None = None
        self.cas_error = KernelError("session_conflict", "fixture CAS conflict")
        self.store = SyncStore(self)
        self.gateway = SyncGateway(self)
        self.runtime = TrustedActionSessionRuntime(
            cast(TrustedActionGateway, self.gateway),
            cast(SessionStore, self.store),
            self.lock_for,
            self.validate,
            self.fault,
        )

    def lock_for(self, thread_id: UUID) -> asyncio.Lock:
        self.lock_lookups += 1
        if self.lock_lookup_hook is not None:
            self.lock_lookup_hook(self.lock_lookups)
        return self.locks[thread_id]

    def visit(self, point: str) -> None:
        self.trace.append(point)
        if isinstance(self.lock, RuntimeThreadLock):
            self.snapshots.append(
                (self.lock._owner, self.lock._owner_generation, self.lock.locked())
            )
        if hook := self.hooks.get(point):
            hook()
        if error := self.errors.get(point):
            raise error

    async def suspend(self, point: str) -> None:
        await asyncio.sleep(0)
        if hook := self.async_hooks.get(point):
            await hook()

    def validate(self, call: ToolCallContent) -> None:
        assert call.call_id == CALL_ID
        self.visit("validate")

    def fault(self, point: str) -> None:
        pytest.fail(f"同步原语不得新增 fault 点: {point}")

    def project(self) -> TrustedActionApprovalRequestContent:
        approval = self.store.thread.turns[0].items[1].content
        assert isinstance(approval, TrustedActionApprovalRequestContent)
        self.gateway.projected = approval.model_copy(
            update={
                "route_state": "ready",
                "decision": ApprovalRecord(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="original-reviewer",
                    request_fingerprint=approval.request_fingerprint,
                    decided_at=DECIDED_AT,
                ),
            }
        )
        return self.gateway.projected

    async def sync(self, borrowed: bool = True) -> Turn | None:
        if borrowed:
            return await self.runtime._sync_decision_in_owned_thread(THREAD_ID, TURN_ID)
        return await self.runtime.sync_decision(THREAD_ID, TURN_ID)


@pytest.fixture
def probe() -> SyncProbe:
    return SyncProbe()


async def test_borrowed_pending_keeps_original_task_and_acquire_generation(
    probe: SyncProbe,
) -> None:
    original = probe.store.thread
    async with probe.lock, asyncio.timeout(2):
        lock = cast(RuntimeThreadLock, probe.lock)
        owner, generation = lock._owner, lock._owner_generation
        assert await probe.sync() is original.turns[0]
        assert lock.locked() and lock._owner is owner
        assert lock._owner_generation is generation
        assert probe.snapshots == [(owner, generation, True)] * 3
    assert probe.trace == ["get_thread", "validate", "gateway"]
    assert not probe.store.appends
    assert probe.store.thread is original


@pytest.mark.parametrize("borrowed", [True, False])
async def test_projection_uses_original_gateway_timestamp_and_cas_once(
    probe: SyncProbe, borrowed: bool
) -> None:
    original = probe.store.thread
    projected = probe.project()
    if borrowed:
        async with probe.lock, asyncio.timeout(2):
            lock = cast(RuntimeThreadLock, probe.lock)
            owner, generation = lock._owner, lock._owner_generation
            updated = await probe.sync()
            assert probe.snapshots == [(owner, generation, True)] * 4
            assert await probe.sync() is None
            assert lock._owner_generation is generation
    else:
        updated = await probe.sync(False)
        assert await probe.sync(False) is None
    assert updated is not None
    assert updated.created_at == original.turns[0].created_at
    assert updated.budget == original.turns[0].budget
    assert updated.items[1].content == projected
    assert updated.items[1].status is ItemStatus.COMPLETED
    assert probe.gateway.received == [
        (
            original,
            original.turns[0],
            original.turns[0].items[0].content,
            original.turns[0].items[1].content,
        )
    ]
    assert probe.trace == ["get_thread", "validate", "gateway", "append", "get_thread"]
    [(thread_id, (draft,), sequence)] = probe.store.appends
    assert thread_id == THREAD_ID and sequence == original.sequence
    assert draft.turn_id == TURN_ID and draft.occurred_at == DECIDED_AT
    assert isinstance(draft.payload, ItemFinished)
    assert draft.payload.item_id == ITEM_ID and draft.payload.content is projected
    assert probe.store.thread.sequence == original.sequence + 1


@pytest.mark.parametrize(
    "case",
    ["not_waiting", "no_call", "no_approval", "generic_approval", "finished", "decided", "settled"],
)
async def test_borrowed_noop_does_not_validate_project_or_append(
    probe: SyncProbe, case: str
) -> None:
    turn = probe.store.thread.turns[0]
    call_item, approval_item = turn.items
    if case == "not_waiting":
        turn = turn.model_copy(update={"status": TurnStatus.EXECUTING_TOOLS})
    elif case == "no_call":
        turn = turn.model_copy(update={"items": (approval_item,)})
    elif case == "no_approval":
        turn = turn.model_copy(update={"items": (call_item,)})
    elif case == "generic_approval":
        approval_item = approval_item.model_copy(
            update={
                "content": ApprovalRequestContent(
                    approval_id=UUID(int=8), call_id=CALL_ID, request_fingerprint="c" * 64
                )
            }
        )
        turn = turn.model_copy(update={"items": (call_item, approval_item)})
    elif case == "finished":
        approval_item = approval_item.model_copy(update={"status": ItemStatus.COMPLETED})
        turn = turn.model_copy(update={"items": (call_item, approval_item)})
    elif case == "decided":
        approval_item = approval_item.model_copy(update={"content": probe.project()})
        turn = turn.model_copy(update={"items": (call_item, approval_item)})
    else:
        result = Item(
            item_id=UUID(int=9),
            status=ItemStatus.COMPLETED,
            content=ToolResultContent(call_id=CALL_ID, outcome="succeeded"),
        )
        turn = turn.model_copy(update={"items": (*turn.items, result)})
    probe.store.thread = probe.store.thread.model_copy(update={"turns": (turn,)})
    async with probe.lock:
        assert await probe.sync() is None
    assert probe.trace == ["get_thread"]
    assert not probe.store.appends and not probe.gateway.received


@pytest.mark.parametrize("projected", [False, True])
async def test_normal_sync_preserves_generic_asyncio_lock_and_narrow_ports(projected: bool) -> None:
    probe = SyncProbe(asyncio.Lock())
    if projected:
        probe.project()
    result = await probe.sync(False)
    assert result is not None
    assert not probe.lock.locked()
    assert len(probe.store.appends) == int(projected)
    assert probe.lock_lookups == 1


@pytest.mark.parametrize("borrowed", [True, False])
@pytest.mark.parametrize("point", ["get_thread", "validate", "gateway", "append"])
@pytest.mark.parametrize("kind", ["kernel", "runtime", "cancelled"])
async def test_original_exception_identity_without_retry_or_fault(
    probe: SyncProbe, borrowed: bool, point: str, kind: str
) -> None:
    probe.project()
    error = (
        KernelError("original_failure", "原端口失败")
        if kind == "kernel"
        else RuntimeError("original failure")
        if kind == "runtime"
        else asyncio.CancelledError("original cancellation")
    )
    probe.errors[point] = error
    async with probe.lock if borrowed else asyncio.Lock():
        with pytest.raises(type(error)) as caught:
            await probe.sync(borrowed)
        assert caught.value is error
        assert probe.lock.locked() is borrowed
    assert (
        probe.trace
        == ["get_thread", "validate", "gateway", "append"][: probe.trace.index(point) + 1]
    )
    assert probe.store.thread.sequence == 9


async def test_original_cas_conflict_is_not_retried_or_translated(probe: SyncProbe) -> None:
    probe.project()
    probe.hooks["append"] = lambda: setattr(
        probe.store, "thread", probe.store.thread.model_copy(update={"sequence": 10})
    )
    async with probe.lock:
        with pytest.raises(KernelError) as caught:
            await probe.sync()
    assert caught.value is probe.cas_error
    assert len(probe.store.appends) == 1
    assert len(probe.gateway.received) == 1


async def test_unheld_lock_rejects_before_store_or_gateway(probe: SyncProbe) -> None:
    with pytest.raises(KernelError) as caught:
        await probe.sync()
    assert caught.value.code == "runtime_thread_lock_unowned"
    assert not probe.trace


@pytest.mark.parametrize("managed", [False, True])
async def test_foreign_child_cannot_sync_even_with_valid_owner_observer(
    probe: SyncProbe, managed: bool
) -> None:
    async with probe.lock:
        lock = cast(RuntimeThreadLock, probe.lock)
        observer = lock.observe_owner()

        async def child() -> None:
            observer()
            with pytest.raises(KernelError) as caught:
                await probe.sync()
            assert caught.value.code == "runtime_thread_lock_unowned"
            observer()

        if managed:
            async with asyncio.TaskGroup() as group:
                group.create_task(child())
        else:
            await asyncio.create_task(child())
        lock.require_current_owner()
    assert not probe.trace


def run_without_task(probe: SyncProbe) -> None:
    coroutine = probe.sync()
    try:
        with pytest.raises(KernelError) as caught:
            coroutine.send(None)
        assert caught.value.code == "runtime_thread_lock_unowned"
    finally:
        coroutine.close()


def test_no_event_loop_rejects_before_store_or_gateway(probe: SyncProbe) -> None:
    run_without_task(probe)
    assert not probe.trace


async def test_no_task_callback_rejects_even_while_original_task_owns_lock(
    probe: SyncProbe,
) -> None:
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    async with probe.lock:

        def callback() -> None:
            try:
                assert asyncio.current_task() is None
                run_without_task(probe)
            except BaseException as error:
                done.set_exception(error)
            else:
                done.set_result(None)

        asyncio.get_running_loop().call_soon(callback)
        await done
    assert not probe.trace


class UncheckedLock(RuntimeThreadLock):
    def require_current_owner(self) -> None:
        pass


@pytest.mark.parametrize("lock_type", [asyncio.Lock, UncheckedLock])
async def test_borrowed_rejects_generic_lock_and_runtime_lock_subclass(
    lock_type: type[asyncio.Lock],
) -> None:
    probe = SyncProbe(lock_type())
    async with probe.lock:
        with pytest.raises(KernelError) as caught:
            await probe.sync()
        assert caught.value.code == "runtime_thread_lock_unowned"
    assert not probe.trace


@pytest.mark.parametrize("point", ["get_thread", "validate", "gateway", "append"])
@pytest.mark.parametrize("drift", ["release", "reacquire", "replace"])
async def test_each_boundary_rejects_lock_identity_owner_and_generation_drift(
    probe: SyncProbe, point: str, drift: str
) -> None:
    probe.project()
    lock = cast(RuntimeThreadLock, probe.lock)

    def mutate() -> None:
        if drift == "replace":
            probe.locks[THREAD_ID] = RuntimeThreadLock()
            return
        lock.release()
        if drift == "reacquire":
            # 无等待者，真实 acquire 必须同步完成；没有修改锁的私有代际字段。
            acquire = lock.acquire()
            try:
                with pytest.raises(StopIteration) as acquired:
                    acquire.send(None)
                assert acquired.value.value is True
            finally:
                acquire.close()

    probe.hooks[point] = mutate
    await lock.acquire()
    original_generation = lock._owner_generation
    try:
        with pytest.raises(KernelError) as caught:
            await probe.sync()
        assert caught.value.code == "runtime_thread_lock_unowned"
        assert (
            probe.trace
            == ["get_thread", "validate", "gateway", "append"][: probe.trace.index(point) + 1]
        )
        assert len(probe.store.appends) <= int(point == "append")
        if drift == "reacquire":
            assert lock._owner is asyncio.current_task()
            assert lock._owner_generation is not original_generation
    finally:
        if lock.locked():
            lock.release()


@pytest.mark.parametrize(
    ("lookup", "expected_trace"),
    [
        (2, []),
        (3, ["get_thread"]),
        (4, ["get_thread", "validate"]),
        (5, ["get_thread", "validate", "gateway"]),
        (6, ["get_thread", "validate", "gateway"]),
        (7, ["get_thread", "validate", "gateway", "append"]),
    ],
)
async def test_factory_replacement_is_checked_before_and_after_each_ledger_boundary(
    probe: SyncProbe, lookup: int, expected_trace: list[str]
) -> None:
    probe.project()
    replacement = RuntimeThreadLock()

    def replace(at: int) -> None:
        if at == lookup:
            probe.locks[THREAD_ID] = replacement

    probe.lock_lookup_hook = replace
    async with probe.lock, replacement:
        # 两把锁都属于原 Task；仍须拒绝工厂换锁，不能只核对当前所有权。
        with pytest.raises(KernelError) as caught:
            await probe.sync()
        assert caught.value.code == "runtime_thread_lock_unowned"
        cast(RuntimeThreadLock, probe.lock).require_current_owner()
        replacement.require_current_owner()
    assert probe.trace == expected_trace
    assert probe.lock_lookups == lookup
    assert len(probe.store.appends) == int(lookup == 7)


async def test_pending_none_projection_still_checks_post_gateway_lock(probe: SyncProbe) -> None:
    probe.hooks["gateway"] = lambda: probe.locks.update({THREAD_ID: RuntimeThreadLock()})
    async with probe.lock:
        with pytest.raises(KernelError) as caught:
            await probe.sync()
        assert caught.value.code == "runtime_thread_lock_unowned"
    assert probe.trace == ["get_thread", "validate", "gateway"]
    assert not probe.store.appends


async def test_noop_still_checks_post_read_lock(probe: SyncProbe) -> None:
    turn = probe.store.thread.turns[0].model_copy(update={"status": TurnStatus.COMPLETED})
    probe.store.thread = probe.store.thread.model_copy(update={"turns": (turn,)})
    probe.hooks["get_thread"] = lambda: probe.locks.update({THREAD_ID: RuntimeThreadLock()})
    async with probe.lock:
        with pytest.raises(KernelError) as caught:
            await probe.sync()
        assert caught.value.code == "runtime_thread_lock_unowned"
    assert probe.trace == ["get_thread"]


@pytest.mark.parametrize("point", ["get_thread", "validate", "gateway", "append"])
async def test_original_exception_wins_when_same_boundary_also_loses_lock(
    probe: SyncProbe, point: str
) -> None:
    probe.project()
    lock = cast(RuntimeThreadLock, probe.lock)
    error = KernelError("original_store_or_gateway_failure", "保留原异常")
    probe.hooks[point] = lock.release
    probe.errors[point] = error
    await lock.acquire()
    try:
        with pytest.raises(KernelError) as caught:
            await probe.sync()
        assert caught.value is error
        assert not lock.locked()
        assert probe.trace.count(point) == 1
    finally:
        if lock.locked():
            lock.release()


@pytest.mark.parametrize("point", ["get_thread", "append"])
async def test_foreign_owner_taking_original_lock_during_io_is_rejected(
    probe: SyncProbe, point: str
) -> None:
    probe.project()
    lock = cast(RuntimeThreadLock, probe.lock)
    acquired, finish = asyncio.Event(), asyncio.Event()

    async def foreign_owner() -> None:
        async with lock:
            acquired.set()
            await finish.wait()

    async def transfer() -> None:
        lock.release()
        task = asyncio.create_task(foreign_owner())
        foreign_tasks.append(task)
        await acquired.wait()

    foreign_tasks: list[asyncio.Task[None]] = []
    probe.async_hooks[point] = transfer
    await lock.acquire()
    try:
        async with asyncio.timeout(2):
            with pytest.raises(KernelError) as caught:
                await probe.sync()
        assert caught.value.code == "runtime_thread_lock_unowned"
        assert lock._owner is foreign_tasks[0]
        assert probe.trace.count(point) == 1
    finally:
        finish.set()
        await asyncio.gather(*foreign_tasks)
        if lock._owner is asyncio.current_task():
            lock.release()
    assert not lock.locked()


@pytest.mark.parametrize("point", ["get_thread", "append"])
async def test_cancellation_during_io_leaves_original_lock_with_caller(
    probe: SyncProbe, point: str
) -> None:
    probe.project()
    started, never = asyncio.Event(), asyncio.Event()

    async def block() -> None:
        started.set()
        await never.wait()

    probe.async_hooks[point] = block

    async def owner() -> None:
        async with probe.lock:
            lock = cast(RuntimeThreadLock, probe.lock)
            generation = lock._owner_generation
            with pytest.raises(asyncio.CancelledError):
                await probe.sync()
            lock.require_current_owner()
            assert lock._owner_generation is generation

    task = asyncio.create_task(owner())
    try:
        async with asyncio.timeout(2):
            await started.wait()
            task.cancel()
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not probe.lock.locked()
    assert probe.store.thread.sequence == 9
    assert probe.trace.count(point) == 1
