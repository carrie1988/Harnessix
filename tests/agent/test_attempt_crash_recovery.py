from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import EventDraft
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed, prepare_attempt


async def crash(store, thread, mode, point):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.agent.attempt_crash_worker",
        str(store.path),
        str(thread.thread_id),
        mode,
        point,
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), 15) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def recover(store, thread):
    provider = FakeProvider()
    async with AgentRuntime(store, provider):
        snapshot = await store.get_thread(thread.thread_id)
    assert not provider.requests
    assert snapshot == replay(await store.events(thread.thread_id))
    assert snapshot.turns[-1].status == "interrupted"
    assert all(a.status != "running" for a in snapshot.turns[-1].model_attempts)
    return snapshot.turns[-1]


@pytest.mark.parametrize("mode", ["start", "usage", "finish", "billing"])
@pytest.mark.parametrize(
    "point", ["session.after_events", "session.after_projection", "session.after_commit"]
)
async def test_attempt_transaction_crash_matrix(tmp_path: Path, mode, point) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepare_attempt(store, tmp_path)
    start = attempt_start()
    payloads = []
    if mode in {"usage", "finish", "billing"}:
        payloads.append(start)
    if mode == "finish":
        payloads.append(observed(start, completeness="complete", input_tokens=10, output_tokens=3))
    if payloads:
        thread = await store.append(
            thread.thread_id,
            [EventDraft(turn_id=thread.active_turn_id, payload=p) for p in payloads],
            expected_sequence=thread.sequence,
        )
    await crash(store, thread, mode, point)
    turn = await recover(store, thread)
    committed = point == "session.after_commit"
    assert len(turn.model_attempts) == int(mode != "start" or committed)
    if turn.model_attempts:
        attempt = turn.model_attempts[0]
        assert attempt.status == ("completed" if mode == "finish" and committed else "interrupted")
        has_usage = mode == "finish" or (mode in {"usage", "billing"} and committed)
        assert attempt.usage.completeness == ("complete" if has_usage else "unknown")
        assert attempt.usage.input_tokens == (10 if has_usage else None)
        assert turn.usage.total_tokens == (13 if has_usage else 0)
        assert attempt.billing.observed == (mode == "billing" and committed)
        if mode == "billing" and committed:
            assert attempt.billing.cache_creation_5m_tokens == 3
            assert attempt.billing.service_tier == "standard"


@pytest.mark.parametrize(
    "point",
    [
        "runtime.after_model_attempt_started",
        "runtime.after_model_usage_observed",
        "runtime.after_model_attempt_finished",
    ],
)
async def test_runtime_crash_never_reissues_model_request(tmp_path: Path, point) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    await crash(store, thread, "runtime", point)
    marker = Path(str(store.path) + ".requests")
    exists = await asyncio.to_thread(marker.exists)
    assert exists == (point != "runtime.after_model_attempt_started")
    turn = await recover(store, thread)
    assert len(turn.model_attempts) == 1
    assert turn.model_attempts[0].status == (
        "completed" if point.endswith("finished") else "interrupted"
    )
    assert turn.usage.total_tokens == (0 if point.endswith("started") else 13)
    if exists:
        assert await asyncio.to_thread(marker.read_text) == "1"


@pytest.mark.parametrize(
    "point", ["session.after_events", "session.after_projection", "session.after_commit"]
)
async def test_recovery_settlement_is_atomic_and_idempotent(tmp_path: Path, point) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepare_attempt(store, tmp_path)
    start = attempt_start()
    thread = await store.append(
        thread.thread_id,
        [
            EventDraft(turn_id=thread.active_turn_id, payload=p)
            for p in [
                start,
                observed(start, completeness="partial", input_tokens=10, output_tokens=1),
            ]
        ],
        expected_sequence=thread.sequence,
    )
    await crash(store, thread, "recovery", point)
    turn = await recover(store, thread)
    assert turn.usage.total_tokens == 11
    assert turn.model_attempts[0].status == "interrupted"
    assert turn.model_attempts[0].usage.completeness == "partial"
    events = await store.events(thread.thread_id)
    assert sum(isinstance(e.payload, ModelAttemptFinished) for e in events) == 1
    await recover(store, thread)
    assert events == await store.events(thread.thread_id)
