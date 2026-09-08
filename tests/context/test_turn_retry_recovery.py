from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseFailed
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore


@pytest.mark.parametrize(
    ("point", "committed"),
    [
        ("session.after_events", False),
        ("session.after_projection", False),
        ("session.after_commit", True),
    ],
)
async def test_retry_acceptance_hard_exit_is_atomic_and_never_replays_provider(
    tmp_path: Path,
    point: str,
    committed: bool,
) -> None:
    path = tmp_path / "session.sqlite"
    store = SQLiteSessionStore(path)
    async with AgentRuntime(
        store, ScriptedProvider([[ResponseFailed(code="authentication")]])
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        source = await runtime.run_turn(thread.thread_id, "失败任务", request_id="source")
    assert source.status is TurnStatus.FAILED

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(Path(__file__).with_name("turn_retry_crash_worker.py")),
            str(path),
            str(thread.thread_id),
            str(source.turn_id),
            point,
        ],
        env=env,
        check=False,
        timeout=15,
    )
    assert result.returncode == 92

    reopened = SQLiteSessionStore(path)
    provider = FakeProvider()
    async with AgentRuntime(reopened, provider) as runtime:
        current = await reopened.get_thread(thread.thread_id)
        assert len(current.turns) == (2 if committed else 1)
        assert current.turns[0] == source
        if committed:
            retried = current.turns[-1]
            assert retried.retry_of_turn_id == source.turn_id
            assert retried.status is TurnStatus.INTERRUPTED
            duplicate = await runtime.retry_turn(
                thread.thread_id,
                source.turn_id,
                request_id="crash-retry",
            )
            assert duplicate == retried

    assert provider.requests == []
    persisted = await reopened.get_thread(thread.thread_id)
    assert replay(await reopened.events(thread.thread_id)) == persisted
    assert await reopened.rebuild(thread.thread_id) == persisted
