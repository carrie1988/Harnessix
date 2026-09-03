from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "point", ["started", "initial_usage", "complete_usage", "finished", "billing"]
)
async def test_sdk_crash_preserves_attempt_and_never_reissues_request(
    tmp_path: Path, kind, point
) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.models.attempt_crash_worker",
        str(store.path),
        str(thread.thread_id),
        kind,
        point,
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), 15) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    provider = FakeProvider()
    async with AgentRuntime(store, provider):
        snapshot = await store.get_thread(thread.thread_id)
    events = await store.events(thread.thread_id)
    assert not provider.requests and snapshot == replay(events)
    turn = snapshot.turns[-1]
    assert turn.status == "interrupted" and len(turn.model_attempts) == 1
    attempt = turn.model_attempts[0]
    assert attempt.status == ("completed" if point == "finished" else "interrupted")
    complete = point in {"complete_usage", "finished"}
    partial = point in {"initial_usage", "billing"} and kind == "anthropic"
    assert attempt.usage.completeness == (
        "complete" if complete else "partial" if partial else "unknown"
    )
    assert turn.usage.total_tokens == (12 if complete else 11 if partial else 0)
    assert turn.usage_is_complete == complete
    assert attempt.billing.observed == (point == "billing")
    if point == "billing":
        assert attempt.billing.service_tier == ("default" if kind == "openai" else "standard")
        if kind == "anthropic":
            assert attempt.billing.cache_creation_5m_tokens == 3
    assert sum(isinstance(e.payload, ModelAttemptFinished) for e in events) == 1
    marker = Path(str(store.path) + ".requests")
    assert await asyncio.to_thread(marker.exists) == (point != "started")
    if point != "started":
        assert await asyncio.to_thread(marker.read_text) == "1"
    async with AgentRuntime(store, provider):
        pass
    assert not provider.requests and events == await store.events(thread.thread_id)
