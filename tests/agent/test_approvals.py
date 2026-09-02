from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from harnessix.agent.approvals import request_fingerprint, tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    ApprovalRequestContent,
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    ToolResultContent,
    Turn,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    RiskLevel,
    utc_now,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step

APPROVE = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="测试用户")
REJECT = ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="测试用户", reason="不需要读取")


def approval(turn: Turn) -> ApprovalRequestContent:
    return next(
        i.content for i in reversed(turn.items) if isinstance(i.content, ApprovalRequestContent)
    )


async def reply(runtime, thread_id, turn, decision=APPROVE, **overrides):
    content = approval(turn)
    args = dict(fingerprint=content.request_fingerprint, decision=decision)
    args.update(overrides)
    return await runtime.reply_approval(thread_id, turn.turn_id, content.approval_id, **args)


async def test_approval_is_durable_before_execution_and_excluded_from_model(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")

    class InspectTool(RecordingTools):
        async def execute(self, call, cancel):
            snapshot = await store.get_thread(thread_id)
            turn = snapshot.turns[-1]
            assert turn.status == TurnStatus.EXECUTING_TOOLS
            assert approval(turn).decision.outcome == ApprovalOutcome.APPROVED
            return await super().execute(call, cancel)

    tools = InspectTool(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        thread_id = thread.thread_id
        turn = await runtime.run_turn(thread_id, "任务", request_id="approval")
        assert turn.status == TurnStatus.WAITING_APPROVAL
        assert tools.calls == []
        assert provider.requests[0].tools[0].requires_approval
        assert (await runtime.resume_turn(thread_id, turn.turn_id)) == turn
        assert await runtime.run_turn(thread_id, "任务", request_id="approval") == turn
        waiting = await store.get_thread(thread_id)
        assert replay(await store.events(thread_id)) == waiting
        with pytest.raises(KernelError):
            await runtime.run_turn(thread_id, "新任务", request_id="other")
        decided = await reply(runtime, thread_id, turn)
        assert decided.status == TurnStatus.WAITING_APPROVAL
        assert tools.calls == []
        sequence = (await store.get_thread(thread_id)).sequence
        assert await reply(runtime, thread_id, turn) == decided
        assert (await store.get_thread(thread_id)).sequence == sequence
        completed = await runtime.resume_turn(thread_id, turn.turn_id)
        assert completed.status == TurnStatus.COMPLETED
        assert len(tools.calls) == 1
        assert len(provider.requests) == 2
        assert not any(
            isinstance(i.content, ApprovalRequestContent) for i in provider.requests[1].history
        )
        assert await runtime.resume_turn(thread_id, turn.turn_id) == completed
        assert await reply(runtime, thread_id, turn) == completed
        assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)


async def test_rejection_and_serial_approvals_preserve_call_pairing(tmp_path: Path) -> None:
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read", "test.read"), answer()])
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        first = approval(turn)
        await reply(runtime, thread.thread_id, turn, REJECT)
        second = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        assert second.status == TurnStatus.WAITING_APPROVAL
        assert approval(second).approval_id != first.approval_id
        assert tools.calls == []
        await reply(runtime, thread.thread_id, second)
        completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
    assert completed.status == TurnStatus.COMPLETED
    assert [c.call_id for c in tools.calls] == [approval(second).call_id]
    results = [i.content for i in completed.items if isinstance(i.content, ToolResultContent)]
    assert [r.call_id for r in results] == [first.call_id, approval(second).call_id]
    assert results[0].error.code == "approval_rejected"
    assert results[1].outcome == "succeeded"


async def test_mismatch_conflict_and_concurrent_replies(tmp_path: Path) -> None:
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([tool_step("test.read"), answer()]),
        RecordingTools(approval=True),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        with pytest.raises(KernelError) as error:
            await reply(runtime, thread.thread_id, turn, fingerprint="0" * 64)
        assert error.value.code == "approval_mismatch"
        with pytest.raises(KernelError) as error:
            await runtime.reply_approval(
                thread.thread_id,
                turn.turn_id,
                new_id(),
                fingerprint=approval(turn).request_fingerprint,
                decision=APPROVE,
            )
        assert error.value.code == "approval_not_found"
        results = await asyncio.gather(
            reply(runtime, thread.thread_id, turn),
            reply(runtime, thread.thread_id, turn, REJECT),
            return_exceptions=True,
        )
        assert sum(isinstance(r, Turn) for r in results) == 1
        errors = [r for r in results if isinstance(r, KernelError)]
        assert len(errors) == 1 and errors[0].code == "approval_conflict"


@pytest.mark.parametrize("decided", [False, True])
async def test_cancel_parked_turn_and_reject_late_reply(tmp_path: Path, decided: bool) -> None:
    tools = RecordingTools(approval=True)
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([tool_step("test.read"), answer()]),
        tools,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        if decided:
            await reply(runtime, thread.thread_id, turn)
        cancelled = await runtime.cancel(thread.thread_id, turn.turn_id)
        assert cancelled.status == TurnStatus.CANCELLED
        assert not pending_calls(cancelled)
        assert all(i.status != ItemStatus.STARTED for i in cancelled.items)
        assert tools.calls == []
        assert await runtime.cancel(thread.thread_id, turn.turn_id) == cancelled
        if not decided:
            with pytest.raises(KernelError) as error:
                await reply(runtime, thread.thread_id, turn)
            assert error.value.code == "approval_closed"
        assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == cancelled


@pytest.mark.parametrize("decided", [False, True])
async def test_restart_preserves_wait_and_only_resumes_pending_call(
    tmp_path: Path, decided: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read", "test.read"), answer()])
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        first = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        await reply(runtime, thread.thread_id, first)
        second = await runtime.resume_turn(thread.thread_id, first.turn_id)
        assert len(tools.calls) == 1
        if decided:
            await reply(runtime, thread.thread_id, second)
        before = await store.get_thread(thread.thread_id)
    provider = ScriptedProvider([tool_step("test.read", "test.read"), answer()])
    fresh_tools = RecordingTools(approval=True)
    async with AgentRuntime(SQLiteSessionStore(store.path), provider, fresh_tools) as runtime:
        assert await store.get_thread(thread.thread_id) == before
        assert provider.requests == [] and fresh_tools.calls == []
        if not decided:
            assert await runtime.resume_turn(thread.thread_id, second.turn_id) == second
            await reply(runtime, thread.thread_id, second)
        completed = await runtime.resume_turn(thread.thread_id, second.turn_id)
        assert completed.status == TurnStatus.COMPLETED
        assert [c.call_id for c in fresh_tools.calls] == [approval(second).call_id]
        assert [r.step for r in provider.requests] == [2]


@pytest.mark.parametrize("change", ["version", "schema", "approval", "effect", "missing"])
@pytest.mark.parametrize("decided", [False, True])
async def test_changed_contract_cannot_reuse_approval(
    tmp_path: Path, change: str, decided: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(
        store, ScriptedProvider([tool_step("test.read"), answer()]), RecordingTools(approval=True)
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        if decided:
            await reply(runtime, thread.thread_id, turn)

    class ChangedTools(RecordingTools):
        def definitions(self):
            original = super().definitions()[0]
            changes = {
                "version": {"version": "2"},
                "schema": {"input_schema": {"type": "object", "required": ["path"]}},
                "approval": {"requires_approval": False},
                "effect": {"effect_class": "idempotent_write"},
            }
            return () if change == "missing" else (original.model_copy(update=changes[change]),)

    tools = ChangedTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, tools) as runtime:
        if not decided:
            with pytest.raises(KernelError) as error:
                await reply(runtime, thread.thread_id, turn)
            assert error.value.code == "tool_contract_changed"
        finished = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        assert finished.status == TurnStatus.FAILED
        assert finished.error.code == "tool_contract_changed"
        assert tools.calls == [] and provider.requests == []


@pytest.mark.parametrize("restart", [False, True])
async def test_wall_clock_budget_includes_approval_wait(
    tmp_path: Path, monkeypatch, restart: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = RecordingTools(approval=True)
    async with AgentRuntime(store, ScriptedProvider([tool_step("test.read")]), tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "任务", request_id="r", budget=Budget(timeout_seconds=60)
        )
        monkeypatch.setattr(
            "harnessix.agent.approvals.utc_now", lambda: turn.created_at + timedelta(seconds=61)
        )
        with pytest.raises(KernelError) as error:
            await reply(runtime, thread.thread_id, turn)
        assert error.value.code == "approval_expired"
        if not restart:
            finished = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert finished.error.code == "time_budget_exceeded"
    async with AgentRuntime(store, ScriptedProvider([answer()]), tools):
        finished = (await store.get_thread(thread.thread_id)).turns[-1]
        assert finished.status == TurnStatus.FAILED
        assert finished.error.code == "time_budget_exceeded"
        assert tools.calls == []


async def test_reducer_refuses_bypassing_decision_and_mutating_request(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(
        store, ScriptedProvider([tool_step("test.read")]), RecordingTools(approval=True)
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        item = next(i for i in turn.items if isinstance(i.content, ApprovalRequestContent))
        snapshot = await store.get_thread(thread.thread_id)
        now = utc_now()
        record = ApprovalRecord(
            **APPROVE.model_dump(),
            request_fingerprint=item.content.request_fingerprint,
            decided_at=now,
        )
        bad_payloads = [
            TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
            ItemStarted(
                item_id=new_id(),
                content=ToolResultContent(call_id=item.content.call_id, outcome="succeeded"),
            ),
            ItemFinished(item_id=item.item_id, status=ItemStatus.COMPLETED, content=item.content),
            ItemFinished(
                item_id=item.item_id,
                status=ItemStatus.COMPLETED,
                content=item.content.model_copy(update={"call_id": new_id(), "decision": record}),
            ),
            ItemFinished(
                item_id=item.item_id,
                status=ItemStatus.COMPLETED,
                content=item.content.model_copy(
                    update={"decision": record.model_copy(update={"request_fingerprint": "0" * 64})}
                ),
            ),
        ]
        for payload in bad_payloads:
            with pytest.raises(KernelError):
                await store.append(
                    thread.thread_id,
                    [EventDraft(turn_id=turn.turn_id, occurred_at=now, payload=payload)],
                    expected_sequence=snapshot.sequence,
                )
            assert await store.get_thread(thread.thread_id) == snapshot
        # 即使直接写入合法拒绝决定，也不能通过 Reducer 伪造成功结果。
        await reply(runtime, thread.thread_id, turn, REJECT)
        snapshot = await store.get_thread(thread.thread_id)
        with pytest.raises(KernelError):
            await store.append(
                thread.thread_id,
                [
                    EventDraft(
                        turn_id=turn.turn_id,
                        payload=TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
                    ),
                    EventDraft(
                        turn_id=turn.turn_id,
                        payload=ItemStarted(
                            item_id=new_id(),
                            content=ToolResultContent(
                                call_id=item.content.call_id, outcome="succeeded"
                            ),
                        ),
                    ),
                ],
                expected_sequence=snapshot.sequence,
            )
        assert await store.get_thread(thread.thread_id) == snapshot


async def test_fingerprint_binds_context_parameters_and_full_tool_contract(tmp_path: Path) -> None:
    tools = RecordingTools(approval=True)
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([tool_step("test.read")]), tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    call = pending_calls(turn)[0]
    value = approval(turn).request_fingerprint
    assert request_fingerprint(thread, turn, call) == value
    assert (
        request_fingerprint(thread.model_copy(update={"workspace": "/different"}), turn, call)
        != value
    )
    assert request_fingerprint(thread, turn.model_copy(update={"turn_id": new_id()}), call) != value
    assert (
        request_fingerprint(thread, turn, call.model_copy(update={"arguments": {"path": "other"}}))
        != value
    )
    assert request_fingerprint(thread, turn, call, policy_version="next-policy") != value
    definition = tools.definitions()[0]
    assert call.tool_fingerprint == tool_fingerprint(definition)
    assert (
        tool_fingerprint(definition.model_copy(update={"risk_level": RiskLevel.HIGH}))
        != call.tool_fingerprint
    )


async def test_concurrent_resume_cannot_dispatch_twice(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowTools(RecordingTools):
        async def execute(self, call, cancel):
            entered.set()
            await release.wait()
            return await super().execute(call, cancel)

    tools = SlowTools(approval=True)
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([tool_step("test.read"), answer()]),
        tools,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        await reply(runtime, thread.thread_id, turn)
        task = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(KernelError):
            await runtime.resume_turn(thread.thread_id, turn.turn_id)
        release.set()
        assert (await task).status == TurnStatus.COMPLETED
        assert len(tools.calls) == 1


@pytest.mark.parametrize("mode", ["api", "task", "close"])
async def test_cancel_resumed_tool_cleans_up_children(tmp_path: Path, mode: str) -> None:
    entered = asyncio.Event()
    stopped = asyncio.Event()

    class BlockingTool(RecordingTools):
        async def execute(self, call, cancel):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(
        store,
        ScriptedProvider([tool_step("test.read"), answer()]),
        BlockingTool(approval=True),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        await reply(runtime, thread.thread_id, turn)
        task = asyncio.create_task(runtime.resume_turn(thread.thread_id, turn.turn_id))
        await asyncio.wait_for(entered.wait(), 2)
        if mode == "api":
            await runtime.cancel(thread.thread_id, turn.turn_id)
            assert (await asyncio.wait_for(task, 2)).status == TurnStatus.CANCELLED
        elif mode == "task":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert stopped.is_set()
    assert task.done()
    finished = (await store.get_thread(thread.thread_id)).turns[-1]
    assert finished.status == TurnStatus.CANCELLED
    assert not pending_calls(finished)


async def test_cancel_racing_with_parking_does_not_leave_cancelling_orphan(tmp_path: Path) -> None:
    parked = asyncio.Event()
    release = asyncio.Event()

    class DelayedStore(SQLiteSessionStore):
        async def append(self, thread_id, drafts, *, expected_sequence):
            result = await super().append(thread_id, drafts, expected_sequence=expected_sequence)
            if any(
                isinstance(d.payload, TurnStateChanged)
                and d.payload.status == TurnStatus.WAITING_APPROVAL
                for d in drafts
            ):
                parked.set()
                await release.wait()
            return result

    store = DelayedStore(tmp_path / "s.db")
    async with AgentRuntime(
        store, ScriptedProvider([tool_step("test.read")]), RecordingTools(approval=True)
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
        await asyncio.wait_for(parked.wait(), 2)
        turn_id = (await store.get_thread(thread.thread_id)).active_turn_id
        cancellation = asyncio.create_task(runtime.cancel(thread.thread_id, turn_id))
        release.set()
        await asyncio.wait_for(cancellation, 2)
        await asyncio.wait_for(task, 2)
        final = (await store.get_thread(thread.thread_id)).turns[-1]
        assert final.status == TurnStatus.CANCELLED


async def test_reply_commit_failure_rolls_back_and_can_be_retried(tmp_path: Path) -> None:
    fail = False

    def inject(point):
        if fail and point == "session.after_projection":
            raise OSError("故障夹具：写入失败")

    store = SQLiteSessionStore(tmp_path / "s.db", fault=inject)
    tools = RecordingTools(approval=True)
    async with AgentRuntime(
        store, ScriptedProvider([tool_step("test.read"), answer()]), tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        before = await store.get_thread(thread.thread_id)
        fail = True
        with pytest.raises(OSError):
            await reply(runtime, thread.thread_id, turn)
        fail = False
        assert await store.get_thread(thread.thread_id) == before
        assert approval(before.turns[-1]).decision is None
        assert tools.calls == []
        await reply(runtime, thread.thread_id, turn)
        completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        assert completed.status == TurnStatus.COMPLETED and len(tools.calls) == 1


async def test_reply_task_cancel_after_commit_keeps_decision_retryable(tmp_path: Path) -> None:
    committed = asyncio.Event()
    release = asyncio.Event()

    class DelayedDecisionStore(SQLiteSessionStore):
        async def append(self, thread_id, drafts, *, expected_sequence):
            result = await super().append(thread_id, drafts, expected_sequence=expected_sequence)
            if any(
                isinstance(d.payload, ItemFinished)
                and isinstance(d.payload.content, ApprovalRequestContent)
                and d.payload.content.decision is not None
                for d in drafts
            ):
                committed.set()
                await release.wait()
            return result

    store = DelayedDecisionStore(tmp_path / "s.db")
    tools = RecordingTools(approval=True)
    async with AgentRuntime(
        store,
        ScriptedProvider([tool_step("test.read"), answer()]),
        tools,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        task = asyncio.create_task(reply(runtime, thread.thread_id, turn))
        await asyncio.wait_for(committed.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tools.calls == []
        decided = await reply(runtime, thread.thread_id, turn)
        assert (
            approval(decided).decision
            == approval((await store.get_thread(thread.thread_id)).turns[-1]).decision
        )
        completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        assert completed.status == TurnStatus.COMPLETED
        assert len(tools.calls) == 1
