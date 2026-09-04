import asyncio
from dataclasses import replace
from threading import Event

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from tests.patches.test_agent_bridge import case as case
from tests.patches.test_kernel_patch import approval_of, decide, patch_step, results


async def test_tiny_model_budget_stops_before_patch_plan(case, tmp_path, monkeypatch):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:

        async def forbidden(*args):
            pytest.fail("提案输出超预算不能准备写计划")

        monkeypatch.setattr(bridge, "prepare", forbidden)
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([patch_step(copy, bridge)]),
            patches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(
                thread.thread_id, "极小预算", request_id="tiny", budget=Budget(max_output_chars=1)
            )
            assert turn.status == TurnStatus.FAILED
            assert not any(i.content.kind == "patch_approval_request" for i in turn.items)
            assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


async def test_approval_expiring_during_review_is_not_committed(case, tmp_path, monkeypatch):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        original = bridge.review

        async def review(*args, **kwargs):
            record = await original(*args, **kwargs)
            monkeypatch.setattr("harnessix.agent.runtime.remaining_seconds", lambda turn: -1)
            return record

        monkeypatch.setattr(bridge, "review", review)
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([patch_step(copy, bridge)]),
            patches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "迟到批准", request_id="late")
            with pytest.raises(KernelError) as error:
                await decide(runtime, thread.thread_id, turn)
            assert error.value.code == "approval_expired"
            saved = (await runtime.store.get_thread(thread.thread_id)).turns[-1]
            assert approval_of(saved).decision is None
            assert copy.get(approval_of(saved).plan.plan_id).state == "pending"


def test_patch_cannot_shadow_registered_tool(case, tmp_path):
    _, _, copy = case
    bridge = ManagedPatchBridge(copy)

    class Duplicate:
        def definitions(self):
            return (bridge.definition(),)

    with pytest.raises(KernelError) as error:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([]), Duplicate(), patches=bridge
        )
    assert error.value.code == "duplicate_tool"


async def test_wrong_workspace_never_falls_back_to_writing(case, tmp_path):
    source, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([patch_step(copy, bridge)]),
            patches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(source.root))
            turn = await runtime.run_turn(thread.thread_id, "错误工作区", request_id="scope")
            assert turn.status == TurnStatus.INTERRUPTED
            assert results(turn)[0].outcome == "unknown"
            assert not any(i.content.kind == "patch_approval_request" for i in turn.items)
    assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("budget", [250, 500])
async def test_output_failure_preserves_private_effect_and_stops_model(
    case, tmp_path, monkeypatch, budget
):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        original = bridge.execute

        async def oversized(*args):
            settled = await original(*args)
            return replace(settled, result=settled.result.model_copy(update={"output": "x" * 1000}))

        monkeypatch.setattr(bridge, "execute", oversized)
        provider = ScriptedProvider([patch_step(copy, bridge)])
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(store, provider, patches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(
                thread.thread_id,
                "结果预算",
                request_id="budget",
                budget=Budget(max_output_chars=budget),
            )
            await decide(runtime, thread.thread_id, turn)
            turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert turn.status == TurnStatus.FAILED and turn.error.code == "tool_output_too_large"
            assert len(provider.requests) == 1
            result = results(turn)[0]
            assert result.outcome == "succeeded"
            assert result.patch.state == "applied" and result.patch.origin == "recovery"
            if budget == 250:
                assert result.output is None
            assert (copy.workspace.root / "main.py").read_bytes() == b"after\r\n"
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )


@pytest.mark.parametrize("operation", ["write", "review"])
async def test_runtime_close_drains_write_and_approval_review_despite_repeated_cancel(
    case, tmp_path, monkeypatch, operation
):
    _, _, copy = case
    entered, release = Event(), Event()

    def hold(at):
        if at == "after_replace":
            entered.set()
            assert release.wait(5)

    async with ManagedPatchBridge(copy) as bridge:
        store = SQLiteSessionStore(tmp_path / "s.db")
        runtime = AgentRuntime(store, ScriptedProvider([patch_step(copy, bridge)]), patches=bridge)
        await runtime.__aenter__()
        thread = await runtime.create_thread(str(copy.workspace.root))
        turn = await runtime.run_turn(thread.thread_id, "关闭排空", request_id="close")
        if operation == "write":
            await decide(runtime, thread.thread_id, turn)
            monkeypatch.setattr(managed, "_fault", hold)
            working = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        else:
            original = copy.verify

            def verify(*args):
                hold("after_replace")
                return original(*args)

            monkeypatch.setattr(copy, "verify", verify)
            working = asyncio.create_task(decide(runtime, thread.thread_id, turn))
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 4)
            closing = asyncio.create_task(runtime.__aexit__(None, None, None))
            for _ in range(15):
                await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            for _ in range(15):
                await asyncio.sleep(0)
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
        async with AgentRuntime(store, ScriptedProvider([]), patches=bridge):
            saved = (await store.get_thread(thread.thread_id)).turns[-1]
            if operation == "write":
                assert saved.status == TurnStatus.CANCELLED
                assert results(saved)[0].outcome == "succeeded"
                assert results(saved)[0].patch.origin == "recovery"
            else:
                assert saved.status == TurnStatus.WAITING_APPROVAL
                assert approval_of(saved).decision is not None
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )


async def test_missing_single_port_cannot_settle_call_inside_waiting_and_spin(case, tmp_path):
    _, _, copy = case
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with ManagedPatchBridge(copy) as bridge:
        async with AgentRuntime(
            store, ScriptedProvider([patch_step(copy, bridge)]), patches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "旧端口丢失", request_id="missing-port")
            await decide(runtime, thread.thread_id, turn)
        provider = ScriptedProvider([])
        async with AgentRuntime(store, provider) as runtime:
            async with asyncio.timeout(2):
                finished = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert finished.status == TurnStatus.INTERRUPTED
            assert results(finished)[0].outcome == "unknown"
            assert not provider.requests
