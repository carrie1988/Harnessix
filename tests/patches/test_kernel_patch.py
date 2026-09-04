import asyncio
from threading import Event
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    PatchApprovalRequestContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.patches.bridge_helpers import make_call
from tests.patches.test_agent_bridge import case as case

APPROVE = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="Kernel 测试宿主")
REJECT = ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="Kernel 测试宿主")


def patch_step(copy, bridge, *, extra=None):
    call, _ = make_call(copy, bridge)
    return [
        ResponseStarted(response_id="patch"),
        ToolCallCompleted(
            call_id="patch-1", tool="apply_patch", arguments={**call.arguments, **(extra or {})}
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def approval_of(turn):
    return next(i.content for i in turn.items if isinstance(i.content, PatchApprovalRequestContent))


def results(turn):
    return [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]


async def decide(runtime, thread_id, turn, decision=APPROVE):
    content = approval_of(turn)
    return await runtime.reply_approval(
        thread_id,
        turn.turn_id,
        content.approval_id,
        fingerprint=content.request_fingerprint,
        decision=decision,
    )


async def test_patch_approval_reopen_execute_and_replay(case, tmp_path):
    source, _, copy = case
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with ManagedPatchBridge(copy) as bridge:
        provider = ScriptedProvider([patch_step(copy, bridge), answer()])
        async with AgentRuntime(store, provider, patches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "修改文件", request_id="patch")
            assert turn.status == TurnStatus.WAITING_APPROVAL
            content = approval_of(turn)
            assert (
                content.plan.thread_id == thread.thread_id and content.plan.turn_id == turn.turn_id
            )
            assert copy.get(content.plan.plan_id).state == "pending"
        # 重开只保留等待，不调用模型；答复只保存 Session 决定。
        reopened_provider = ScriptedProvider([[], answer()])
        async with AgentRuntime(store, reopened_provider, patches=bridge) as runtime:
            assert not reopened_provider.requests
            decided = await decide(runtime, thread.thread_id, turn)
            assert await decide(runtime, thread.thread_id, turn) == decided
            assert copy.get(content.plan.plan_id).state == "pending"
            assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"
            completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert completed.status == TurnStatus.COMPLETED
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == completed
        assert results(completed)[0].outcome == "succeeded"
        assert results(completed)[0].patch.state == "applied"
        assert results(completed)[0].patch.origin == "execution"
        assert (copy.workspace.root / "main.py").read_bytes() == b"after\r\n"
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )
    assert (source.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("changed", [False, True])
async def test_reject_does_not_need_stale_source_and_never_writes(case, tmp_path, changed):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([patch_step(copy, bridge), answer()]),
            patches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "拒绝修改", request_id="reject")
            target = copy.workspace.root / "main.py"
            if changed:
                target.write_bytes(b"external")
            await decide(runtime, thread.thread_id, turn, REJECT)
            with pytest.raises(KernelError) as error:
                await decide(runtime, thread.thread_id, turn, APPROVE)
            assert error.value.code == "approval_conflict"
            finished = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert finished.status == TurnStatus.COMPLETED
            assert (
                results(finished)[0].outcome == "failed"
                and results(finished)[0].patch.state == "rejected"
            )
            assert target.read_bytes() == (b"external" if changed else b"before\r\n")


@pytest.mark.parametrize("bad", ["fingerprint", "stale", "expired"])
async def test_invalid_approval_never_reaches_backend_decision(case, tmp_path, monkeypatch, bad):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([patch_step(copy, bridge)]),
            patches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "验证审批", request_id="invalid")
            content = approval_of(turn)
            if bad == "stale":
                (copy.workspace.root / "main.py").write_text("changed")
            if bad == "expired":
                monkeypatch.setattr("harnessix.agent.runtime.remaining_seconds", lambda t: -1)
            with pytest.raises(KernelError) as error:
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    content.approval_id,
                    fingerprint="0" * 64 if bad == "fingerprint" else content.request_fingerprint,
                    decision=APPROVE,
                )
            assert (
                error.value.code
                == {
                    "fingerprint": "approval_mismatch",
                    "stale": "patch_source_changed",
                    "expired": "approval_expired",
                }[bad]
            )
            assert copy.get(content.plan.plan_id).state == "pending"


async def test_model_injected_approval_returns_fixable_tool_failure(case, tmp_path):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([patch_step(copy, bridge, extra={"approved": True}), answer()]),
            patches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "拒绝参数注入", request_id="args")
        assert turn.status == TurnStatus.COMPLETED
        assert results(turn)[0].error.code == "tool_invalid_arguments"
        assert not any(isinstance(i.content, PatchApprovalRequestContent) for i in turn.items)
        assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "other"),
        ("effect_class", EffectClass.READ_ONLY),
        ("requires_approval", False),
        ("requires_idempotency", False),
        ("supports_reconciliation", False),
    ],
)
def test_special_port_contract_is_mandatory(case, tmp_path, field, value):
    _, _, copy = case
    bridge = ManagedPatchBridge(copy)
    definition = bridge.definition().model_copy(update={field: value})
    bridge.definition = lambda: definition
    with pytest.raises(KernelError) as error:
        AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([]), patches=bridge)
    assert error.value.code == "patch_contract_invalid"


async def test_callback_failure_after_file_effect_preserves_success_without_completed_turn(
    case, tmp_path
):
    _, _, copy = case

    def fault(point):
        if point == "runtime.after_tool":
            raise RuntimeError("注入回调错误")

    async with ManagedPatchBridge(copy) as bridge:
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(
            store, ScriptedProvider([patch_step(copy, bridge)]), patches=bridge, fault=fault
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "提交窗口", request_id="window")
            await decide(runtime, thread.thread_id, turn)
            finished = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert finished.status == TurnStatus.FAILED
            assert (
                results(finished)[0].outcome == "succeeded"
                and results(finished)[0].patch.origin == "recovery"
            )
            assert (copy.workspace.root / "main.py").read_bytes() == b"after\r\n"
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )


@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("mode", ["token", "task", "repeated", "timeout"])
async def test_kernel_cancel_drains_then_keeps_actual_effect(
    case, tmp_path, monkeypatch, point, mode
):
    _, _, copy = case
    blocked, release, finished = Event(), Event(), Event()

    def hold(at):
        if at == point:
            blocked.set()
            assert release.wait(5)
        if at == "result_recorded":
            finished.set()

    monkeypatch.setattr(managed, "_fault", hold)
    async with ManagedPatchBridge(copy) as bridge:
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(
            store, ScriptedProvider([patch_step(copy, bridge)]), patches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(
                thread.thread_id,
                "取消写入",
                request_id="cancel",
                budget=Budget(timeout_seconds=0.4 if mode == "timeout" else 120),
            )
            await decide(runtime, thread.thread_id, turn)
            task = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
            try:
                assert await asyncio.to_thread(blocked.wait, 4)
                if mode == "token":
                    await runtime.cancel(thread.thread_id, turn.turn_id)
                elif mode == "timeout":
                    await asyncio.sleep(0.45)
                else:
                    task.cancel()
                for _ in range(15):
                    await asyncio.sleep(0)
                if mode == "repeated":
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                assert not task.done() and not finished.is_set()
            finally:
                release.set()
            if mode in {"task", "repeated"}:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            snapshot = await store.get_thread(thread.thread_id)
            settled = snapshot.turns[-1]
            assert finished.is_set()
            assert settled.status == (
                TurnStatus.FAILED if mode == "timeout" else TurnStatus.CANCELLED
            )
            assert results(settled)[0].outcome == (
                "succeeded" if point == "after_replace" else "failed"
            )
            assert results(settled)[0].patch.origin == "recovery"
            assert replay(await store.events(thread.thread_id)) == snapshot


async def test_new_events_reject_legacy_tags_and_forged_effects(case, tmp_path):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(
            store, ScriptedProvider([patch_step(copy, bridge), answer()]), patches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(thread.thread_id, "契约", request_id="schema")
            await decide(runtime, thread.thread_id, turn)
            await runtime.resume_turn(thread.thread_id, turn.turn_id)
        events = await store.events(thread.thread_id)
        affected = [
            e
            for e in events
            if isinstance(e.payload, ItemStarted | ItemFinished)
            and (
                isinstance(e.payload.content, PatchApprovalRequestContent)
                or isinstance(e.payload.content, ToolResultContent)
                and e.payload.content.patch is not None
            )
        ]
        for event in affected:
            for version in range(1, 6):
                with pytest.raises(ValidationError):
                    EventDraft(schema_version=version, payload=event.payload)
        target = next(
            e
            for e in events
            if isinstance(e.payload, ItemStarted)
            and isinstance(e.payload.content, ToolResultContent)
        )
        content = target.payload.content
        for effect in (
            None,
            content.patch.model_copy(update={"plan_id": uuid4()}),
            content.patch.model_copy(update={"request_id": "0" * 64}),
            content.patch.model_copy(update={"state": "pending"}),
        ):
            altered = target.model_copy(
                update={
                    "payload": target.payload.model_copy(
                        update={"content": content.model_copy(update={"patch": effect})}
                    )
                }
            )
            with pytest.raises(KernelError):
                replay([altered if e == target else e for e in events])
