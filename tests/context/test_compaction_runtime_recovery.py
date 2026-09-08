from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import CompactionWindowActivated, EventDraft
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
from harnessix.context.compaction_window import build_compaction_window
from harnessix.domain.models import utc_now
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.context.compaction_ledger_helpers import append, prepare_source
from tests.context.test_compaction import summary


@pytest.mark.parametrize(
    "point",
    [
        "runtime.after_compaction_planned",
        "runtime.after_compaction_attempt_started",
        "runtime.after_compaction_usage_observed",
        "runtime.after_compaction_attempt_finished",
        "runtime.after_compaction_summarized",
        "runtime.after_compaction_window_activated",
    ],
)
async def test_runtime_compaction_crash_never_reissues_paid_summary(
    tmp_path: Path, point: str
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with AgentRuntime(store, FakeProvider("长历史事实与约束。\n" * 1000)) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        seeded = await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        assert seeded.status == "completed"

    marker = tmp_path / "summary-requests"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.context.compaction_runtime_crash_worker",
        str(store.path),
        str(thread.thread_id),
        point,
        str(marker),
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

    request_sent = marker.exists()
    assert request_sent == (
        point
        not in {
            "runtime.after_compaction_planned",
            "runtime.after_compaction_attempt_started",
        }
    )

    reopened = SQLiteSessionStore(store.path)
    provider = FakeProvider()
    async with AgentRuntime(reopened, provider):
        recovered = await reopened.get_thread(thread.thread_id)
    assert not provider.requests
    assert recovered == replay(await reopened.events(thread.thread_id))
    turn = recovered.turns[-1]
    assert turn.status == "interrupted"
    record = turn.compactions[0]
    if point.endswith("summarized") or point.endswith("window_activated"):
        assert record.status == "summarized"
        assert record.attempt.status == "completed"
        assert len(recovered.compaction_windows) == 1
        assert recovered.compaction_windows[0].compaction_id == record.plan.compaction_id
    else:
        assert record.status == "interrupted"
        assert not recovered.compaction_windows
        if point.endswith("planned"):
            assert record.attempt is None
        else:
            assert record.attempt is not None
            assert record.attempt.status == (
                "completed" if point.endswith("attempt_finished") else "interrupted"
            )

    events = await reopened.events(thread.thread_id)
    async with AgentRuntime(SQLiteSessionStore(store.path), provider):
        assert events == await reopened.events(thread.thread_id)
    assert not provider.requests


@pytest.mark.parametrize(
    "point", ["session.after_events", "session.after_projection", "session.after_commit"]
)
async def test_window_activation_transaction_crash_recovers_exactly_once(
    tmp_path: Path, point: str
) -> None:
    store = SQLiteSessionStore(tmp_path / "window-transaction.sqlite")
    source, prepared = await prepare_source(store, tmp_path)
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    attempt = ModelAttemptStarted(
        attempt_id=uuid4(),
        step=prepared.plan.model_step,
        index=1,
        provider="fixture",
        requested_model="summary",
    )
    identity = prepared.plan.compaction_id
    source = await append(
        store,
        source,
        CompactionPlanned(plan=prepared.plan, decisions=prepared.model_history.new_decisions),
        CompactionAttemptStarted(compaction_id=identity, event=attempt),
        CompactionUsageObserved(
            compaction_id=identity,
            event=ModelUsageObserved(
                attempt_id=attempt.attempt_id,
                actual_model="summary",
                response_id="response",
                usage=UsageObservation(completeness="complete", input_tokens=20, output_tokens=5),
            ),
        ),
        CompactionAttemptFinished(
            compaction_id=identity,
            event=ModelAttemptFinished(attempt_id=attempt.attempt_id, outcome="completed"),
        ),
        CompactionSummarized(
            compaction_id=identity,
            summary=summary(prepared),
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ),
    )
    record = source.turns[-1].compactions[-1]
    occurred_at = utc_now()
    window = build_compaction_window(
        source,
        record,
        candidate,
        window_id=uuid4(),
        activated_event_sequence=source.sequence + 1,
        activated_at=occurred_at,
    )
    payload_path = tmp_path / "window-event.json"
    payload_path.write_text(
        EventDraft(
            turn_id=source.active_turn_id,
            occurred_at=occurred_at,
            payload=CompactionWindowActivated(window=window),
        ).model_dump_json()
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.context.compaction_ledger_crash_worker",
        str(store.path),
        str(source.thread_id),
        str(payload_path),
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

    reopened = SQLiteSessionStore(store.path)
    provider = FakeProvider()
    async with AgentRuntime(reopened, provider):
        recovered = await reopened.get_thread(source.thread_id)
    events = await reopened.events(source.thread_id)
    assert not provider.requests
    assert recovered == replay(events)
    assert len(recovered.compaction_windows) == 1
    assert recovered.active_compaction_window_id == recovered.compaction_windows[0].window_id
    assert recovered.compaction_windows[0].compaction_id == identity
    assert sum(event.payload.type == "compaction_window_activated" for event in events) == 1
