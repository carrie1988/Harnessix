import asyncio
from threading import Event

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import batch_agent_bridge
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.tools.workspace import ReadOperation
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.bridge_helpers import approval
from tests.patches.test_batch_diff_bridge import ledger_state
from tests.patches.test_bridge_cancel import entered
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot


@pytest.mark.parametrize("view", ["plan", "effect"])
@pytest.mark.parametrize("mode", ["token", "task", "repeated", "timeout", "close"])
async def test_report_worker_drains_before_cancel_or_close(group_case, monkeypatch, view, mode):
    _, _, copy, _, prepared = group_case
    blocked, release, finished = Event(), Event(), Event()
    original = batch_agent_bridge.batch_diff_document

    def held(*args, **kwargs):
        blocked.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        extra = {}
        if view == "effect":
            decision = approval(plan)
            result = await bridge.execute(call, scope, plan, decision, CancelToken())
            extra = {"view": view, "approval": decision, "execution": result.execution}
        before = ledger_state(copy), snapshot(copy.workspace.root)
        monkeypatch.setattr(batch_agent_bridge, "batch_diff_document", held)
        token, deadline = CancelToken(), asyncio.timeout(None)

        async def render():
            async with deadline:
                return await bridge.diff(call, scope, plan, token, **extra)

        task = asyncio.create_task(render())
        closing = None
        try:
            await entered(blocked)
            if mode == "token":
                token.cancel()
            elif mode == "timeout":
                deadline.reschedule(asyncio.get_running_loop().time())
            elif mode == "close":
                closing = asyncio.create_task(bridge.aclose())
            else:
                task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            if mode == "repeated":
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            if closing is not None:
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
                assert not closing.done()
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        if closing is not None:
            await task
            with pytest.raises(asyncio.CancelledError):
                await closing
            with pytest.raises(KernelError):
                await bridge.diff(call, scope, plan, CancelToken())
        else:
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
        assert (ledger_state(copy), snapshot(copy.workspace.root)) == before


@pytest.mark.parametrize("mode", ["token", "deadline"])
async def test_stopped_operation_never_loads_ledger(group_case, monkeypatch, mode):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())

        def forbidden(*args):
            pytest.fail("取消/过期不能加载账本")

        monkeypatch.setattr(bridge._groups, "_load", forbidden)
        token = CancelToken()
        if mode == "token":
            token.cancel()
        else:

            class Expired(ReadOperation):
                def __init__(self):
                    super().__init__()
                    self.deadline = 0

            monkeypatch.setattr(batch_agent_bridge, "ReadOperation", Expired)
        with pytest.raises(TurnCancelled if mode == "token" else KernelError):
            await bridge.diff(call, scope, plan, token)
