from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import ApprovalRequestContent, ItemStatus, TurnStatus
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.approval_crash_worker import ApprovalCountingTool
from tests.agent.helpers import answer, tool_step


@pytest.mark.parametrize(
    ("point", "resumable", "decided", "count"),
    [
        ("request.after_events", False, False, 0),
        ("request.after_projection", False, False, 0),
        ("runtime.after_approval_request", True, False, 0),
        ("decision.after_events", True, False, 0),
        ("decision.after_projection", True, False, 0),
        ("runtime.after_approval_decision", True, True, 0),
        ("runtime.after_approval_consumed", False, True, 0),
        ("runtime.before_tool", False, True, 0),
        ("runtime.after_tool", False, True, 1),
        ("runtime.before_terminal", False, True, 1),
    ],
)
async def test_approval_crash_boundaries(
    tmp_path: Path,
    point: str,
    resumable: bool,
    decided: bool,
    count: int,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    counter = tmp_path / "count"
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.agent.approval_crash_worker",
        str(store.path),
        str(thread.thread_id),
        point,
        str(counter),
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), timeout=10) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, ApprovalCountingTool(counter)) as runtime:
        turn = (await store.get_thread(thread.thread_id)).turns[-1]
        assert (int(counter.read_text()) if counter.exists() else 0) == count
        assert provider.requests == []
        if resumable:
            assert turn.status == TurnStatus.WAITING_APPROVAL
            approval = next(
                i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
            )
            assert (approval.decision is not None) == decided
            if not decided:
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="恢复测试"),
                )
            completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert completed.status == TurnStatus.COMPLETED
            assert [request.step for request in provider.requests] == [2]
            assert int(counter.read_text()) == 1
        else:
            assert turn.status == TurnStatus.INTERRUPTED
            assert not pending_calls(turn)
            assert all(i.status != ItemStatus.STARTED for i in turn.items)
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == turn
            assert (int(counter.read_text()) if counter.exists() else 0) == count
        assert replay(await store.events(thread.thread_id)) == await store.get_thread(
            thread.thread_id
        )
