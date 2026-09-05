import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent import batch_patching
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    EventDraft,
    ItemFinished,
    ItemStarted,
    PatchBatchApprovalRequestContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import managed
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import ReadOperation
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import approval_of, batch_step, decide
from tests.patches.test_kernel_patch import APPROVE, REJECT, results
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case


async def test_batch_waiting_reopen_decision_consumption_execution_and_replay(group_case, tmp_path):
    source, factory, copy, groups, prepared = group_case
    original = snapshot(source.root)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            store, ScriptedProvider([batch_step(copy, bridge, prepared)]), patch_batches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "修改三个文件", request_id="batch")
            assert turn.status == TurnStatus.WAITING_APPROVAL
            request = approval_of(turn)
            assert (request.plan.thread_id, request.plan.turn_id) == (
                thread.thread_id,
                turn.turn_id,
            )
            assert request.plan.backend.workspace_id == copy.workspace_id
            assert groups.get(request.plan.backend.batch_id, ReadOperation()).decision is None
        provider = ScriptedProvider([[], answer()])
        async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
            assert not provider.requests
            decided = await decide(runtime, thread.thread_id, turn)
            assert await decide(runtime, thread.thread_id, turn) == decided
            assert groups.get(request.plan.backend.batch_id, ReadOperation()).decision is None
            with pytest.raises(KernelError):
                call = next(i.content for i in turn.items if i.content.kind == "tool_call")
                batch_patching.execution_approval(
                    await store.get_thread(thread.thread_id), decided, call
                )
            completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert completed.status == TurnStatus.COMPLETED
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == completed
            result = results(completed)[0]
            assert result.outcome == "succeeded" and result.patch is None
            assert (
                result.patch_batch.execution.effect == "applied"
                and result.patch_batch.origin == "execution"
            )
            assert len(result.patch_batch.model_dump_json().encode()) < 8192
            assert len(provider.requests) == 1
    workspace_id = copy.workspace_id
    copy.close()
    with factory.open(workspace_id) as reopened:
        async with ManagedPatchBatchBridge(reopened) as bridge:
            provider = ScriptedProvider([])
            async with AgentRuntime(store, provider, patch_batches=bridge):
                assert not provider.requests
                assert (await store.get_thread(thread.thread_id)).turns[-1] == completed
                assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                    thread.thread_id
                )
            assert all((reopened.workspace.root / p).read_bytes() == b"after\r\n" for p in PATHS)
    assert snapshot(source.root) == original


@pytest.mark.parametrize("changed", [False, True])
async def test_reject_only_persists_decision_without_group_run(group_case, tmp_path, changed):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([batch_step(copy, bridge, prepared), answer()]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "拒绝", request_id="reject")
            if changed:
                (copy.workspace.root / PATHS[1]).write_text("changed")
            before = snapshot(copy.workspace.root)
            await decide(runtime, thread.thread_id, waiting, REJECT)
            with pytest.raises(KernelError) as error:
                await decide(runtime, thread.thread_id, waiting, APPROVE)
            assert error.value.code == "approval_conflict"
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            assert turn.status == TurnStatus.COMPLETED
            result = results(turn)[0]
            assert result.outcome == "failed" and result.error.code == "approval_rejected"
            assert result.patch_batch.execution is None
            assert (
                groups.get_execution(approval_of(turn).plan.backend.batch_id, ReadOperation())
                is None
            )
            assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("when", ["review", "execution"])
async def test_stale_source_never_writes_any_member(group_case, tmp_path, when, position):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        provider = ScriptedProvider([batch_step(copy, bridge, prepared)])
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, patch_batches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "漂移", request_id="stale")
            if when == "execution":
                await decide(runtime, thread.thread_id, waiting)
            (copy.workspace.root / PATHS[position]).write_text("other")
            before = snapshot(copy.workspace.root)
            if when == "review":
                with pytest.raises(KernelError) as error:
                    await decide(runtime, thread.thread_id, waiting)
                assert error.value.code == "patch_source_changed"
                assert (
                    groups.get(approval_of(waiting).plan.backend.batch_id, ReadOperation()).decision
                    is None
                )
            else:
                turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                assert turn.status == TurnStatus.FAILED and len(provider.requests) == 1
                assert results(turn)[0].patch_batch.execution.effect == "not_applied"
                assert results(turn)[0].patch_batch.execution.run.stop_reason == "failed"
            assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize(
    "arguments",
    [{"approved": True}, {"files": []}, {"files": [True]}, {"files": [], "scope": "/else"}],
)
async def test_invalid_input_is_fixable_and_does_not_prepare(group_case, tmp_path, arguments):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([batch_step(copy, bridge, prepared, arguments=arguments), answer()]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "无效提案", request_id="args")
        assert turn.status == TurnStatus.COMPLETED
        assert results(turn)[0].error.code == "tool_invalid_arguments"
        assert not any(isinstance(i.content, PatchBatchApprovalRequestContent) for i in turn.items)
        assert copy._db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "apply_patch"),
        ("effect_class", EffectClass.READ_ONLY),
        ("requires_approval", False),
        ("requires_idempotency", False),
        ("supports_reconciliation", False),
    ],
)
def test_batch_special_port_mandatory(group_case, tmp_path, field, value):
    _, _, copy, _, _ = group_case
    bridge = ManagedPatchBatchBridge(copy)
    definition = bridge.definition().model_copy(update={field: value})
    bridge.definition = lambda: definition
    with pytest.raises(KernelError) as error:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([]), patch_batches=bridge
        )
    assert error.value.code == "patch_batch_contract_invalid"


@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("position", range(3))
async def test_partial_and_unknown_stop_turn_with_honest_effect(
    group_case, tmp_path, monkeypatch, point, position
):
    _, _, copy, _, prepared = group_case
    count = 0

    def fault(at):
        nonlocal count
        if at == point:
            index, count = count, count + 1
            if index == position:
                raise OSError("成员故障")

    async with ManagedPatchBatchBridge(copy) as bridge:
        provider = ScriptedProvider([batch_step(copy, bridge, prepared)])
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "故障", request_id="failure")
            await decide(runtime, thread.thread_id, waiting)
            monkeypatch.setattr(managed, "_fault", fault)
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            assert len(provider.requests) == 1
            result = results(turn)[0]
            assert result.patch_batch.execution.effect == (
                "unknown" if point == "after_replace" else "partial" if position else "not_applied"
            )
            assert turn.status == (
                TurnStatus.INTERRUPTED if point == "after_replace" else TurnStatus.FAILED
            )
            assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                thread.thread_id
            )
            assert all(
                m.state == "pending" for m in result.patch_batch.execution.members[position + 1 :]
            )


@pytest.mark.parametrize("point", ["runtime.after_tool", "runtime.after_tool_result"])
async def test_callback_failure_preserves_effect_without_completed_turn(
    group_case, tmp_path, point
):
    _, _, copy, _, prepared = group_case

    def fault(at):
        if at == point:
            raise RuntimeError("回调丢失")

    async with ManagedPatchBatchBridge(copy) as bridge:
        provider = ScriptedProvider([batch_step(copy, bridge, prepared)])
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, patch_batches=bridge, fault=fault
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "丢回调", request_id="callback")
            await decide(runtime, thread.thread_id, waiting)
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert turn.status == TurnStatus.FAILED and len(provider.requests) == 1
        assert results(turn)[0].outcome == "succeeded"
        assert results(turn)[0].patch_batch.origin == (
            "recovery" if point == "runtime.after_tool" else "execution"
        )


async def test_new_event_tags_and_forged_effect_rejected(group_case, tmp_path):
    _, _, copy, _, prepared = group_case
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            store,
            ScriptedProvider([batch_step(copy, bridge, prepared), answer()]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "严格契约", request_id="schema")
            await decide(runtime, thread.thread_id, waiting)
            await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    events = await store.events(thread.thread_id)
    affected = [
        e
        for e in events
        if isinstance(e.payload, ItemStarted | ItemFinished)
        and (
            isinstance(e.payload.content, PatchBatchApprovalRequestContent)
            or isinstance(e.payload.content, ToolResultContent)
            and e.payload.content.patch_batch is not None
        )
    ]
    for event in affected:
        for version in range(1, 7):
            with pytest.raises(ValidationError):
                EventDraft(schema_version=version, payload=event.payload)
    target = next(
        e
        for e in affected
        if isinstance(e.payload, ItemStarted) and isinstance(e.payload.content, ToolResultContent)
    )
    content = target.payload.content
    for effect in [
        None,
        content.patch_batch.model_copy(update={"batch_id": uuid4()}),
        content.patch_batch.model_copy(update={"request_id": "0" * 64}),
        content.patch_batch.model_copy(update={"execution": None}),
    ]:
        changed = target.model_copy(
            update={
                "payload": target.payload.model_copy(
                    update={"content": content.model_copy(update={"patch_batch": effect})}
                )
            }
        )
        with pytest.raises(KernelError):
            replay([changed if e == target else e for e in events])
    dumped = json.dumps([e.model_dump(mode="json") for e in events])
    assert '"schema_version": 9' in dumped
