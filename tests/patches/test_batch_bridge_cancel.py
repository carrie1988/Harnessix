import asyncio
from threading import Event

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import batch_agent_bridge, managed
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.tools.workspace import ReadOperation
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.bridge_helpers import approval
from tests.patches.test_bridge_cancel import entered
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("mode", ["token", "task", "timeout", "repeated"])
async def test_cancel_drains_each_member_and_stops_suffix(
    group_case, monkeypatch, position, point, mode
):
    source, _, copy, _, prepared = group_case
    blocked, release, finished = Event(), Event(), Event()
    count = 0

    def hold(at):
        nonlocal count
        if at == point:
            index, count = count, count + 1
            if index == position:
                blocked.set()
                assert release.wait(5)
        if at == "result_recorded" and blocked.is_set():
            finished.set()

    monkeypatch.setattr(managed, "_fault", hold)
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        token, timeout = CancelToken(), asyncio.timeout(None)

        async def execute():
            async with timeout:
                return await bridge.execute(call, scope, plan, approval(plan), token)

        task = asyncio.create_task(execute())
        try:
            await entered(blocked)
            if mode == "token":
                token.cancel()
            elif mode == "timeout":
                timeout.reschedule(asyncio.get_running_loop().time())
            else:
                task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            if mode == "repeated":
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        error = (
            TurnCancelled
            if mode == "token"
            else TimeoutError
            if mode == "timeout"
            else asyncio.CancelledError
        )
        with pytest.raises(error):
            await task
        assert finished.is_set()
        before = snapshot(copy.workspace.root)
        result = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        applied = position + int(point == "after_replace")
        assert result.execution.effect == (
            "applied" if applied == 3 else "partial" if applied else "not_applied"
        )
        assert result.execution.run.stop_reason == "cancelled"
        assert all(m.state == "pending" for m in result.execution.members[position + 1 :])
        assert snapshot(copy.workspace.root) == before
        for index, path in enumerate(PATHS):
            assert (copy.workspace.root / path).read_bytes() == (
                b"after\r\n" if index < applied else b"before\r\n"
            )
    assert all((source.root / path).read_bytes() == b"before\r\n" for path in PATHS)


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_close_drains_active_rejects_queued_and_is_repeatable(
    group_case, monkeypatch, cancel_close
):
    _, _, copy, _, prepared = group_case
    blocked, release = Event(), Event()

    def hold(at):
        if at == "after_replace" and not blocked.is_set():
            blocked.set()
            assert release.wait(5)

    monkeypatch.setattr(managed, "_fault", hold)
    bridge = ManagedPatchBatchBridge(copy)
    call, scope = make_call(copy, bridge, prepared)
    plan = await bridge.prepare(call, scope, CancelToken())
    task = asyncio.create_task(bridge.execute(call, scope, plan, approval(plan), CancelToken()))
    await entered(blocked)
    queued = asyncio.create_task(bridge.review(call, scope, plan, CancelToken()))
    closing = asyncio.create_task(bridge.aclose())
    also_closing = asyncio.create_task(bridge.aclose())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
        assert not closing.done() and not also_closing.done() and not task.done()
    finally:
        release.set()
    assert (await task).result.outcome == "succeeded"
    if cancel_close:
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await closing
    await also_closing
    await bridge.aclose()
    with pytest.raises(KernelError) as error:
        await queued
    assert error.value.code == "tool_runtime_closed"
    with pytest.raises(KernelError):
        await bridge.__aenter__()
    assert all(copy.get(m.plan_id).state == "applied" for m in plan.backend.members)


@pytest.mark.parametrize("kind", ["cancel", "timeout"])
@pytest.mark.parametrize("method", ["prepare", "review", "execute", "recover"])
async def test_pre_admission_stop_performs_no_backend_io(group_case, monkeypatch, kind, method):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        original = snapshot(copy.workspace.root)
        token = CancelToken()
        if kind == "cancel":
            token.cancel()
        else:

            class Expired(ReadOperation):
                def __init__(self):
                    super().__init__()
                    self.deadline = 0

            monkeypatch.setattr(batch_agent_bridge, "ReadOperation", Expired)
        arguments = {
            "prepare": (call, scope, token),
            "review": (call, scope, plan, token),
            "execute": (call, scope, plan, approval(plan), token),
            "recover": (call, scope, token),
        }
        with pytest.raises(TurnCancelled if kind == "cancel" else KernelError):
            await getattr(bridge, method)(*arguments[method])
        assert snapshot(copy.workspace.root) == original
        assert groups.get(plan.backend.batch_id, ReadOperation()).decision is None
        assert groups.get_execution(plan.backend.batch_id, ReadOperation()) is None


async def test_queue_does_not_refresh_deadline_or_dispatch(group_case, monkeypatch):
    _, _, copy, _, prepared = group_case
    operations = []

    class Captured(ReadOperation):
        def __init__(self):
            super().__init__()
            operations.append(self)

    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        monkeypatch.setattr(batch_agent_bridge, "ReadOperation", Captured)
        async with bridge._lock:
            task = asyncio.create_task(bridge.prepare(call, scope, CancelToken()))
            for _ in range(10):
                await asyncio.sleep(0)
            assert len(operations) == 1 and not task.done()
            operations[0].deadline = 0
        with pytest.raises(KernelError) as error:
            await task
        assert error.value.code == "patch_timeout"
        assert bridge._groups.lookup(bridge._request(scope), ReadOperation()) is None


async def test_cancel_between_group_decision_and_intent_never_replays(group_case, monkeypatch):
    _, _, copy, groups, prepared = group_case
    blocked, release = Event(), Event()
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        original = bridge._groups.reply

        def hold(*args):
            result = original(*args)
            blocked.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(bridge._groups, "reply", hold)
        task = asyncio.create_task(bridge.execute(call, scope, plan, approval(plan), CancelToken()))
        try:
            await entered(blocked)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert groups.get_execution(plan.backend.batch_id, ReadOperation()) is None
        result = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        assert result.result.outcome == "failed" and result.execution is None
        assert result.result.output["phase"] == "not_started"
        assert groups.get_execution(plan.backend.batch_id, ReadOperation()) is None


@pytest.mark.parametrize("method", ["prepare", "review"])
@pytest.mark.parametrize("mode", ["token", "task"])
async def test_prepare_and_review_cancellation_drain_without_approval(
    group_case, monkeypatch, method, mode
):
    _, _, copy, groups, prepared = group_case
    blocked, release, finished = Event(), Event(), Event()
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken()) if method == "review" else None
        original = bridge._groups.verify

        def hold(*args):
            blocked.set()
            assert release.wait(5)
            try:
                return original(*args)
            finally:
                finished.set()

        monkeypatch.setattr(bridge._groups, "verify", hold)
        token = CancelToken()
        task = asyncio.create_task(
            bridge.prepare(call, scope, token)
            if method == "prepare"
            else bridge.review(call, scope, plan, token)
        )
        try:
            await entered(blocked)
            token.cancel() if mode == "token" else task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(TurnCancelled if mode == "token" else asyncio.CancelledError):
            await task
        assert finished.is_set()
        backend = groups.lookup(bridge._request(scope), ReadOperation())
        assert backend.decision is None
        assert groups.get_execution(backend.plan.batch_id, ReadOperation()) is None
        assert all((copy.workspace.root / p).read_bytes() == b"before\r\n" for p in PATHS)
