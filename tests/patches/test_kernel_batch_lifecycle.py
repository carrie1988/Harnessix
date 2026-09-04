import asyncio
from threading import Event

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import managed
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import ReadOperation
from tests.patches.kernel_batch_helpers import approval_of, batch_step, decide
from tests.patches.test_kernel_patch import results
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot


@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("mode", ["token", "task", "repeated", "timeout"])
async def test_kernel_group_cancel_drains_and_preserves_prefix(
    group_case, tmp_path, monkeypatch, point, position, mode
):
    source, _, copy, _, prepared = group_case
    blocked, release, done = Event(), Event(), Event()
    index = -1

    def hold(at):
        nonlocal index
        if at == "started":
            index += 1
        if at == point and index == position:
            blocked.set()
            assert release.wait(5)
        if at == "result_recorded" and blocked.is_set():
            done.set()

    original = snapshot(source.root)
    async with ManagedPatchBatchBridge(copy) as bridge:
        store = SQLiteSessionStore(tmp_path / "s.db")
        provider = ScriptedProvider([batch_step(copy, bridge, prepared)])
        async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(
                thread.thread_id,
                "取消整组",
                request_id="cancel",
                budget=Budget(timeout_seconds=1 if mode == "timeout" else 120),
            )
            await decide(runtime, thread.thread_id, waiting)
            monkeypatch.setattr(managed, "_fault", hold)
            task = asyncio.create_task(runtime.resume_turn(thread.thread_id, waiting.turn_id))
            try:
                assert await asyncio.to_thread(blocked.wait, 4)
                if mode == "token":
                    await runtime.cancel(thread.thread_id, waiting.turn_id)
                elif mode == "timeout":
                    await asyncio.sleep(1.05)
                else:
                    task.cancel()
                for _ in range(15):
                    await asyncio.sleep(0)
                if mode == "repeated":
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                assert not task.done() and not done.is_set()
            finally:
                release.set()
            if mode in {"task", "repeated"}:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            saved = await store.get_thread(thread.thread_id)
            turn = saved.turns[-1]
            assert done.is_set() and len(provider.requests) == 1
            assert turn.status == (TurnStatus.FAILED if mode == "timeout" else TurnStatus.CANCELLED)
            effect = results(turn)[0].patch_batch
            applied = position + int(point == "after_replace")
            assert effect.origin == "recovery"
            assert effect.execution.effect == (
                "applied" if applied == 3 else "partial" if applied else "not_applied"
            )
            assert effect.execution.run.stop_reason == "cancelled"
            assert all(m.state == "pending" for m in effect.execution.members[position + 1 :])
            assert replay(await store.events(thread.thread_id)) == saved
    assert snapshot(source.root) == original


@pytest.mark.parametrize("operation", ["write", "review"])
async def test_close_drains_group_and_review_with_repeated_close_cancellation(
    group_case, tmp_path, monkeypatch, operation
):
    _, _, copy, _, prepared = group_case
    blocked, release = Event(), Event()

    def hold(at):
        if at == "after_replace" and not blocked.is_set():
            blocked.set()
            assert release.wait(5)

    async with ManagedPatchBatchBridge(copy) as bridge:
        store = SQLiteSessionStore(tmp_path / "s.db")
        runtime = AgentRuntime(
            store, ScriptedProvider([batch_step(copy, bridge, prepared)]), patch_batches=bridge
        )
        await runtime.__aenter__()
        thread = await runtime.create_thread(str(copy.workspace.root))
        waiting = await runtime.run_turn(thread.thread_id, "关闭", request_id="close")
        if operation == "write":
            await decide(runtime, thread.thread_id, waiting)
            monkeypatch.setattr(managed, "_fault", hold)
            working = asyncio.create_task(runtime.resume_turn(thread.thread_id, waiting.turn_id))
        else:
            original = bridge._groups.verify

            def verify(*args):
                hold("after_replace")
                return original(*args)

            monkeypatch.setattr(bridge._groups, "verify", verify)
            working = asyncio.create_task(decide(runtime, thread.thread_id, waiting))
        closing = None
        try:
            assert await asyncio.to_thread(blocked.wait, 4)
            closing = asyncio.create_task(runtime.__aexit__(None, None, None))
            for _ in range(15):
                await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            assert not closing.done() and not working.done()
            with pytest.raises(KernelError) as error:
                async with AgentRuntime(SQLiteSessionStore(store.path), ScriptedProvider([])):
                    pass
            assert error.value.code == "runtime_busy"
        finally:
            release.set()
            await working
            if closing is not None:
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await runtime.__aexit__(None, None, None)
        await runtime.__aexit__(None, None, None)
        async with AgentRuntime(store, ScriptedProvider([]), patch_batches=bridge):
            turn = (await store.get_thread(thread.thread_id)).turns[-1]
            if operation == "write":
                assert turn.status == TurnStatus.CANCELLED
                assert results(turn)[0].patch_batch.execution.effect == "partial"
            else:
                assert (
                    turn.status == TurnStatus.WAITING_APPROVAL
                    and approval_of(turn).decision is not None
                )


async def test_review_deadline_drains_without_persisting_decision(
    group_case, tmp_path, monkeypatch
):
    _, _, copy, groups, prepared = group_case
    blocked, release, done = Event(), Event(), Event()
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([batch_step(copy, bridge, prepared)]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(
                thread.thread_id, "复核超时", request_id="review", budget=Budget(timeout_seconds=1)
            )
            original = bridge._groups.verify

            def hold(*args):
                blocked.set()
                assert release.wait(5)
                try:
                    return original(*args)
                finally:
                    done.set()

            monkeypatch.setattr(bridge._groups, "verify", hold)
            task = asyncio.create_task(decide(runtime, thread.thread_id, waiting))
            try:
                assert await asyncio.to_thread(blocked.wait, 4)
                await asyncio.sleep(1.05)
                assert not task.done() and not done.is_set()
            finally:
                release.set()
            with pytest.raises(KernelError) as error:
                await task
            assert error.value.code == "approval_expired" and done.is_set()
            saved = (await runtime.store.get_thread(thread.thread_id)).turns[-1]
            assert approval_of(saved).decision is None
            assert (
                groups.get(approval_of(saved).plan.backend.batch_id, ReadOperation()).decision
                is None
            )


async def test_review_finishing_after_original_deadline_cannot_commit(
    group_case, tmp_path, monkeypatch
):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        original = bridge.review

        async def late(*args, **kwargs):
            result = await original(*args, **kwargs)
            monkeypatch.setattr("harnessix.agent.runtime.remaining_seconds", lambda turn: -1)
            return result

        monkeypatch.setattr(bridge, "review", late)
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([batch_step(copy, bridge, prepared)]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "迟到", request_id="late")
            with pytest.raises(KernelError) as error:
                await decide(runtime, thread.thread_id, waiting)
            assert error.value.code == "approval_expired"
            assert (
                approval_of((await runtime.store.get_thread(thread.thread_id)).turns[-1]).decision
                is None
            )


@pytest.mark.parametrize("decided", [False, True])
async def test_cancel_waiting_without_backend_proof_is_conservatively_unknown(
    group_case, tmp_path, decided
):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([batch_step(copy, bridge, prepared)]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "停止等待", request_id="waiting")
            if decided:
                await decide(runtime, thread.thread_id, waiting)
            before = snapshot(copy.workspace.root)
            cancelled = await runtime.cancel(thread.thread_id, waiting.turn_id)
            assert (
                cancelled.status == TurnStatus.INTERRUPTED
                and results(cancelled)[0].outcome == "unknown"
            )
            assert (
                groups.get_execution(approval_of(waiting).plan.backend.batch_id, ReadOperation())
                is None
            )
            assert snapshot(copy.workspace.root) == before
