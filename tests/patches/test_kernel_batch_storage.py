import sqlite3

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemFinished, ItemStarted, PatchBatchApprovalRequestContent
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import ReadOperation
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import approval_of, batch_step, decide
from tests.patches.test_kernel_patch import results
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot


@pytest.mark.parametrize("point", ["session.after_projection", "session.after_commit"])
@pytest.mark.parametrize("phase", ["decision", "result"])
async def test_session_storage_failure_preserves_approval_and_effect(
    group_case, tmp_path, monkeypatch, phase, point
):
    source, _, copy, groups, prepared = group_case
    original_source = snapshot(source.root)
    store = SQLiteSessionStore(tmp_path / "s.db")
    append = store.append
    injected = False

    async def fail_selected(thread_id, drafts, *, expected_sequence):
        nonlocal injected
        selected = any(
            (
                isinstance(d.payload, ItemFinished)
                and isinstance(d.payload.content, PatchBatchApprovalRequestContent)
                and d.payload.content.decision is not None
            )
            if phase == "decision"
            else (
                isinstance(d.payload, ItemStarted)
                and d.payload.content.kind == "tool_result"
                and d.payload.content.patch_batch is not None
            )
            for d in drafts
        )

        def fault(actual):
            nonlocal injected
            if selected and not injected and actual == point:
                injected = True
                raise sqlite3.OperationalError("整组 Session 存储故障")

        store._fault = fault
        try:
            return await append(thread_id, drafts, expected_sequence=expected_sequence)
        finally:
            store._fault = lambda _: None

    monkeypatch.setattr(store, "append", fail_selected)
    async with ManagedPatchBatchBridge(copy) as bridge:
        provider = ScriptedProvider([batch_step(copy, bridge, prepared), answer()])
        async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "存储故障", request_id="storage")
            plan = approval_of(waiting).plan.backend
            if phase == "decision":
                before = snapshot(copy.workspace.root)
                with pytest.raises(KernelError):
                    await decide(runtime, thread.thread_id, waiting)
                saved = (await store.get_thread(thread.thread_id)).turns[-1]
                assert (approval_of(saved).decision is not None) == (
                    point == "session.after_commit"
                )
                assert groups.get(plan.batch_id, ReadOperation()).decision is None
                assert groups.get_execution(plan.batch_id, ReadOperation()) is None
                assert snapshot(copy.workspace.root) == before
            await decide(runtime, thread.thread_id, waiting)
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            assert injected
            assert turn.status == ("completed" if phase == "decision" else "failed")
            assert len(provider.requests) == (2 if phase == "decision" else 1)
            assert len(results(turn)) == 1 and results(turn)[0].outcome == "succeeded"
            effect = results(turn)[0].patch_batch
            assert effect.execution.effect == "applied"
            assert effect.origin == (
                "recovery"
                if phase == "result" and point == "session.after_projection"
                else "execution"
            )
            assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                thread.thread_id
            )
        assert snapshot(source.root) == original_source
