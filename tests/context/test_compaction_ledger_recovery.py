from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import EventDraft, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.context.compaction import validate_compaction
from harnessix.context.compaction_ledger_contracts import (
    CompactionAttemptFinished,
    CompactionAttemptStarted,
    CompactionPlanned,
    CompactionSummarized,
    CompactionUsageObserved,
)
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.context.compaction_ledger_helpers import append, prepare_source
from tests.context.test_compaction import summary


async def crash(store, thread, payload, point):
    payload_path = store.path.with_suffix(".event.json")
    if payload is not None:
        payload_path.write_text(
            EventDraft(turn_id=thread.active_turn_id, payload=payload).model_dump_json()
        )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.context.compaction_ledger_crash_worker",
        str(store.path),
        str(thread.thread_id),
        str(payload_path) if payload is not None else "recovery",
        point,
        cwd=Path(__file__).parents[2],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 77, (stdout, stderr)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def recover(store, thread):
    reopened = SQLiteSessionStore(store.path)
    provider = FakeProvider()
    async with AgentRuntime(reopened, provider):
        snapshot = await reopened.get_thread(thread.thread_id)
    assert not provider.requests
    assert snapshot == replay(await reopened.events(thread.thread_id))
    assert snapshot.turns[-1].status == TurnStatus.INTERRUPTED
    events = await reopened.events(thread.thread_id)
    async with AgentRuntime(SQLiteSessionStore(store.path), provider):
        assert events == await reopened.events(thread.thread_id)
    return snapshot.turns[-1]


@pytest.mark.parametrize("mode", ["plan", "start", "partial", "finish", "candidate", "recovery"])
@pytest.mark.parametrize(
    "point", ["session.after_events", "session.after_projection", "session.after_commit"]
)
async def test_summary_transaction_crash_and_idempotent_recovery(tmp_path, mode, point):
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    thread, prepared = await prepare_source(store, tmp_path)
    source = thread
    identity = prepared.plan.compaction_id
    start = ModelAttemptStarted(
        attempt_id=uuid4(), step=1, index=1, provider="fixture", requested_model="summary"
    )
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    stages = (
        CompactionPlanned(plan=prepared.plan, decisions=prepared.model_history.new_decisions),
        CompactionAttemptStarted(compaction_id=identity, event=start),
        CompactionUsageObserved(
            compaction_id=identity,
            event=ModelUsageObserved(
                attempt_id=start.attempt_id,
                actual_model="summary",
                response_id="response",
                usage=UsageObservation(completeness="partial", input_tokens=100),
            ),
        ),
        CompactionUsageObserved(
            compaction_id=identity,
            event=ModelUsageObserved(
                attempt_id=start.attempt_id,
                actual_model="summary",
                response_id="response",
                usage=UsageObservation(completeness="complete", input_tokens=100, output_tokens=30),
            ),
        ),
        CompactionAttemptFinished(
            compaction_id=identity,
            event=ModelAttemptFinished(
                attempt_id=start.attempt_id,
                outcome="completed",
            ),
        ),
        CompactionSummarized(
            compaction_id=identity,
            summary=summary(prepared),
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ),
    )
    index = {"plan": 0, "start": 1, "partial": 2, "finish": 4, "candidate": 5, "recovery": 3}[mode]
    if index:
        thread = await append(store, thread, *stages[:index])
    await crash(store, thread, None if mode == "recovery" else stages[index], point)
    turn = await recover(store, thread)
    committed = point == "session.after_commit"
    count = index + int(committed and mode != "recovery")
    assert len(turn.compactions) == int(count > 0)
    assert turn.items[:-1] == source.turns[-1].items  # 仅增加正式终止Error Item。
    assert turn.model_steps == 0 and not turn.model_attempts
    if turn.compactions:
        record = turn.compactions[-1]
        assert record.status == ("summarized" if count == 6 else "interrupted")
        assert (record.attempt is not None) == (count >= 2)
        if record.attempt:
            assert record.attempt.status == ("completed" if count >= 5 else "interrupted")
            assert record.attempt.usage.completeness == (
                "complete" if count >= 4 else "partial" if count >= 3 else "unknown"
            )
    assert turn.usage.total_tokens == (130 if count >= 4 else 100 if count >= 3 else 0)
