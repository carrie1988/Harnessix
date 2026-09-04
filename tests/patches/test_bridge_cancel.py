import asyncio
from threading import Event

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import ReadOperation
from tests.patches.bridge_helpers import approval, make_call
from tests.patches.test_agent_bridge import case as case


async def entered(event):
    assert await asyncio.to_thread(event.wait, 5), "后台操作未到达切点"


@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("mode", ["token", "task", "timeout", "repeated"])
async def test_cancellation_drains_worker_then_recovers_honest_effect(
    case, monkeypatch, point, mode
):
    source, _, copy = case
    blocked, release, finished = Event(), Event(), Event()

    def hold(at):
        if at == point:
            blocked.set()
            assert release.wait(5)
        if at == "result_recorded":
            finished.set()

    monkeypatch.setattr(managed, "_fault", hold)
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        token = CancelToken()
        timeout = asyncio.timeout(None)

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
            # 确认取消传播到正在运行的后台操作后，父任务仍在等待收尾。
            for _ in range(10):
                await asyncio.sleep(0)
            if mode == "repeated":
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        expected_error = (
            TurnCancelled
            if mode == "token"
            else TimeoutError
            if mode == "timeout"
            else asyncio.CancelledError
        )
        with pytest.raises(expected_error):
            await task
        assert finished.is_set()
        result = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        expected = "failed" if point == "before_replace" else "applied"
        assert result.record.state == expected
        assert result.result.outcome == ("failed" if expected == "failed" else "succeeded")
        assert (copy.workspace.root / "main.py").read_bytes() == (
            b"before\r\n" if expected == "failed" else b"after\r\n"
        )
    assert (source.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_close_waits_for_active_worker_and_rejects_queued_operations(
    case, monkeypatch, cancel_close
):
    _, _, copy = case
    blocked, release = Event(), Event()

    def hold(at):
        if at == "after_replace":
            blocked.set()
            assert release.wait(5)

    monkeypatch.setattr(managed, "_fault", hold)
    bridge = ManagedPatchBridge(copy)
    call, scope = make_call(copy, bridge)
    plan = await bridge.prepare(call, scope, CancelToken())
    task = asyncio.create_task(bridge.execute(call, scope, plan, approval(plan), CancelToken()))
    await entered(blocked)
    queued = asyncio.create_task(bridge.review(call, scope, plan, CancelToken()))
    closing = asyncio.create_task(bridge.aclose())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
        assert not closing.done() and not task.done()
    finally:
        release.set()
    assert (await task).result.outcome == "succeeded"
    if cancel_close:
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await closing
    with pytest.raises(KernelError) as error:
        await queued
    assert error.value.code == "tool_runtime_closed"
    with pytest.raises(KernelError):
        await bridge.__aenter__()
    # 桥接不释放宿主所有权；宿主仍能读取已提交结果。
    assert copy.get(plan.plan_id).state == "applied"


async def test_pre_cancelled_prepare_leaves_no_plan(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        token = CancelToken()
        token.cancel()
        with pytest.raises(TurnCancelled):
            await bridge.prepare(call, scope, token)
        assert copy.lookup(bridge._request(scope), ReadOperation()) is None


async def test_cancel_after_backend_decision_before_intent_is_not_started(case, monkeypatch):
    _, _, copy = case
    blocked, release = Event(), Event()
    original = copy.reply

    def hold(*args):
        result = original(*args)
        blocked.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(copy, "reply", hold)
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        task = asyncio.create_task(bridge.execute(call, scope, plan, approval(plan), CancelToken()))
        await entered(blocked)
        task.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        assert result.record.state == "approved" and result.result.outcome == "failed"
        assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


async def test_cooperative_deadline_never_creates_plan(case, monkeypatch):
    _, _, copy = case
    from harnessix.patches import agent_bridge

    class Expired(ReadOperation):
        def __init__(self):
            super().__init__()
            self.deadline = 0

    monkeypatch.setattr(agent_bridge, "ReadOperation", Expired)
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, scope, CancelToken())
        assert error.value.code == "patch_timeout"
        assert copy.lookup(bridge._request(scope), ReadOperation()) is None


async def test_backend_lookup_and_verify_honor_operation(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
    for action in (
        lambda op: copy.lookup(plan.request_id, op),
        lambda op: copy.verify(plan.plan_id, op),
    ):
        stopped = ReadOperation()
        stopped.stopped.set()
        with pytest.raises(TurnCancelled):
            action(stopped)
        expired = ReadOperation()
        expired.deadline = 0
        with pytest.raises(KernelError) as error:
            action(expired)
        assert error.value.code == "patch_timeout"
    with pytest.raises(ReadToolError):
        expired.checkpoint()
