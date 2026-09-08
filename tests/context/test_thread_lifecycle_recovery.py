from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid5

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


@pytest.mark.parametrize(
    ("operation", "point", "committed"),
    [
        ("fork", "session.fork.after_source", False),
        ("fork", "session.after_events", False),
        ("fork", "session.after_projection", False),
        ("fork", "session.after_commit", True),
        ("archive", "session.after_events", False),
        ("archive", "session.after_projection", False),
        ("archive", "session.after_commit", True),
    ],
)
async def test_lifecycle_hard_exit_is_atomic_and_recoverable(
    tmp_path: Path, operation: str, point: str, committed: bool
) -> None:
    path = tmp_path / "session.sqlite"
    store = SQLiteSessionStore(path)
    async with AgentRuntime(store, FakeProvider()) as runtime:
        source = await runtime.create_thread(str(tmp_path))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(Path(__file__).with_name("thread_lifecycle_crash_worker.py")),
            str(path),
            str(source.thread_id),
            operation,
            point,
        ],
        env=env,
        check=False,
        timeout=15,
    )
    assert result.returncode == 91

    reopened = SQLiteSessionStore(path)
    await reopened.initialize()
    provider = FakeProvider()
    async with AgentRuntime(reopened, provider):
        pass
    assert provider.requests == []

    if operation == "fork":
        child_id = uuid5(source.thread_id, "harnessix.thread-fork/v1:crash-fork")
        ids = await reopened.thread_ids()
        assert (child_id in ids) is committed
        if committed:
            child = await reopened.get_thread(child_id)
            assert child.fork_snapshot is not None
            assert await reopened.rebuild(child_id) == child
    else:
        current = await reopened.get_thread(source.thread_id)
        assert (current.archive is not None) is committed
        assert await reopened.rebuild(source.thread_id) == current
