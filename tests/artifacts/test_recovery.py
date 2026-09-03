from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import results, step


@pytest.mark.parametrize("tool", ["glob", "grep"])
@pytest.mark.parametrize(
    "point,published",
    [
        ("runtime.after_tool", False),
        ("artifact.after_insert", False),
        ("session.after_events", False),
        ("session.after_projection", False),
        ("artifact.before_commit", False),
        ("artifact.after_commit", True),
        ("runtime.before_terminal", True),
    ],
)
async def test_process_crash_keeps_blob_result_atomic_without_reexecution(
    tmp_path, tool, point, published
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n")
    session = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(session, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(root.resolve()))
    counter = tmp_path / "count"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.artifacts.crash_worker",
        str(session.path),
        str(thread.thread_id),
        str(root),
        point,
        str(counter),
        tool,
        cwd=Path(__file__).parents[2],
    )
    try:
        assert await asyncio.wait_for(process.wait(), 20) == 77
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert counter.read_text() == "1"
    provider = FakeProvider()
    artifacts = SQLiteArtifactStore(session)

    class NoReexecution(CodingToolRuntime):
        async def execute_scoped(self, *args):
            pytest.fail("恢复不得再次执行已消费调用")

    async with NoReexecution(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            session, provider, scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            turn = (await session.get_thread(thread.thread_id)).turns[-1]
            assert turn.status == TurnStatus.INTERRUPTED
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == turn
            assert not provider.requests and counter.read_text() == "1"
        refs = [r.output["artifact"] for r in results(turn) if r.outcome == "succeeded"]
        assert len(refs) == int(published)
        with sqlite3.connect(session.path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == len(refs)
        if published:
            page = await artifacts.read(
                thread.thread_id, tools.workspace_scope, UUID(refs[0]["artifact_id"])
            )
            assert "main.py" in page.text
    assert replay(await session.events(thread.thread_id)) == await session.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
@pytest.mark.parametrize("kind", ["task", "user"])
async def test_cancel_linearizes_with_artifact_transaction(tmp_path, monkeypatch, point, kind):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    entered, release = asyncio.Event(), asyncio.Event()
    armed = False

    def arm(name):
        nonlocal armed
        if name == "artifact.after_insert":
            armed = True

    session = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(session, fault=arm)
    original_append, original_publish = session._append_in_transaction, artifacts.publish

    async def append(*args, **kwargs):
        nonlocal armed
        result = await original_append(*args, **kwargs)
        if armed and point == "before_commit":
            armed = False
            entered.set()
            await release.wait()
        return result

    async def publish(*args, **kwargs):
        result = await original_publish(*args, **kwargs)
        if point == "after_commit":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(session, "_append_in_transaction", append)
    monkeypatch.setattr(artifacts, "publish", publish)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            session, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "发布期间取消", request_id="cancel")
            )
            cancel_task = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                turn = (await session.get_thread(thread.thread_id)).turns[-1]
                if kind == "task":
                    task.cancel()
                else:
                    # 用户取消在同一 Thread 锁后线性化，允许先完成这次原子提交。
                    cancel_task = asyncio.create_task(
                        runtime.cancel(thread.thread_id, turn.turn_id)
                    )
                    await asyncio.sleep(0)
                    release.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
                if cancel_task:
                    await cancel_task
            finally:
                release.set()
                await asyncio.gather(
                    task, *([cancel_task] if cancel_task else []), return_exceptions=True
                )
    final = (await session.get_thread(thread.thread_id)).turns[-1]
    assert final.status == TurnStatus.CANCELLED
    published = point == "after_commit" or kind == "user"
    assert len([r for r in results(final) if r.outcome == "succeeded"]) == int(published)
    with sqlite3.connect(session.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == int(published)
    assert replay(await session.events(thread.thread_id)) == await session.get_thread(
        thread.thread_id
    )
