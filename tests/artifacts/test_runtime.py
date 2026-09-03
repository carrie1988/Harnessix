from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import ArtifactPolicy
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools import search
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.agent.test_approvals import APPROVE, REJECT, reply
from tests.artifacts.helpers import exercise, results, step


async def test_default_definitions_unchanged_and_artifact_policy_is_versioned(tmp_path):
    session = SQLiteSessionStore(tmp_path / "s.db")
    first, second = (
        SQLiteArtifactStore(session),
        SQLiteArtifactStore(session, policy=ArtifactPolicy(ttl_seconds=60)),
    )
    async with (
        CodingToolRuntime(tmp_path) as legacy,
        CodingToolRuntime(tmp_path, artifacts=first) as enabled,
        CodingToolRuntime(tmp_path, artifacts=second) as changed,
    ):
        old = {d.name: d for d in legacy.definitions()}
        new = {d.name: d for d in enabled.definitions()}
        later = {d.name: d for d in changed.definitions()}
        assert set(new) - set(old) == {"read_artifact"}
        for name in ("list_files", "read_file"):
            assert old[name] == new[name] == later[name]
        for name in ("glob", "grep"):
            assert len({old[name].version, new[name].version, later[name].version}) == 3
        assert new["read_artifact"].version != later["read_artifact"].version
    with pytest.raises(AttributeError):
        first.policy = second.policy


@pytest.mark.parametrize("kind", ["legacy", "other_session", "other_publisher", "missing"])
async def test_misconfigured_publisher_is_not_silently_used(tmp_path, kind):
    session = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(session)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        if kind in {"legacy", "other_session"}:
            with pytest.raises(KernelError) as error:
                AgentRuntime(
                    session,
                    FakeProvider(),
                    artifacts=artifacts
                    if kind == "legacy"
                    else SQLiteArtifactStore(SQLiteSessionStore(session.path)),
                    **({"tools": tools} if kind == "legacy" else {"scoped_tools": tools}),
                )
            assert error.value.code == "artifact_store_mismatch"
        else:
            configured = None if kind == "missing" else SQLiteArtifactStore(session)
            async with AgentRuntime(
                session,
                ScriptedProvider([step(), answer()]),
                scoped_tools=tools,
                artifacts=configured,
            ) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                turn = await runtime.run_turn(thread.thread_id, "配置错误", request_id="mismatch")
            assert turn.error.code == (
                "artifact_not_enabled" if kind == "missing" else "artifact_store_mismatch"
            )


@pytest.mark.parametrize("budget", ["MAX_ARTIFACT_BYTES", "MAX_ARTIFACT_RECORDS"])
async def test_capture_hard_budget_publishes_nothing(tmp_path, monkeypatch, budget):
    monkeypatch.setattr(search, budget, 1)
    session, _, _, _, turn = await exercise(tmp_path)
    assert results(turn)[0].error.code == "tool_limit_exceeded"
    with sqlite3.connect(session.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0


async def test_glob_archive_and_incomplete_grep_are_truthful(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("needle")
    (root / "bad.py").write_bytes(b"needle\x00")
    (root / ".env").write_text("PRIVATE needle")
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            ScriptedProvider([step("glob", pattern="*.py", max_results=1), step(), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "完整性", request_id="gap")
        first, second = [result.output for result in results(turn)]
        assert first["artifact"]["complete"] and first["artifact"]["records"] == 4
        assert not second["artifact"]["complete"] and second["artifact"]["records"] == 3
        page = await artifacts.read(
            thread.thread_id, tools.workspace_scope, UUID(second["artifact"]["artifact_id"])
        )
        assert "PRIVATE" not in page.text
        assert [json.loads(line)["path"] for line in page.text.splitlines()] == [
            "a.py",
            "b.py",
            "c.py",
        ]


async def test_gc_protects_active_thread_and_cursor_does_not_starve_others(tmp_path, monkeypatch):
    store, artifacts, _, first_thread, _ = await exercise(tmp_path, count=1)
    root = tmp_path / "repo"
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            second_thread = await runtime.create_thread(str(tools.workspace_root))
            await runtime.run_turn(second_thread.thread_id, "第二会话", request_id="other")
    with sqlite3.connect(store.path) as db:
        protected_owner = UUID(
            db.execute(
                "SELECT thread_id FROM agent_artifacts ORDER BY artifact_id LIMIT 1"
            ).fetchone()[0]
        )
    assert protected_owner in {first_thread.thread_id, second_thread.thread_id}
    future = artifact_sqlite.utc_now() + timedelta(days=2)
    monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
    async with CodingToolRuntime(root, artifacts=artifacts, require_approval=True) as tools:
        async with AgentRuntime(
            store, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            waiting = await runtime.run_turn(protected_owner, "保持活跃", request_id="waiting")
            assert waiting.status == TurnStatus.WAITING_APPROVAL
            first = await artifacts.collect(limit=1)
            assert first.protected == 1 and first.expired == 0 and first.next_after is not None
            second = await artifacts.collect(limit=1, after=first.next_after)
            assert second.expired == 1
            await runtime.cancel(protected_owner, waiting.turn_id)
            assert (await artifacts.collect()).expired == 1


async def test_approval_reopen_captures_only_after_matching_decision(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with CodingToolRuntime(
        root, artifacts=SQLiteArtifactStore(store), require_approval=True
    ) as tools:
        async with AgentRuntime(
            store, ScriptedProvider([step(), answer()]), scoped_tools=tools
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            waiting = await runtime.run_turn(thread.thread_id, "审批归档", request_id="approval")
            assert waiting.status == TurnStatus.WAITING_APPROVAL
    reopened = SQLiteSessionStore(store.path)
    artifacts = SQLiteArtifactStore(reopened)
    async with CodingToolRuntime(root, artifacts=artifacts, require_approval=True) as tools:
        async with AgentRuntime(
            reopened, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            await reply(runtime, thread.thread_id, waiting)
            completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    assert (
        completed.status == TurnStatus.COMPLETED
        and results(completed)[0].output["artifact"]["records"] == 1
    )


@pytest.mark.parametrize("kind", ["user", "task"])
async def test_cancel_during_capture_leaves_no_artifact(tmp_path, monkeypatch, kind):
    from threading import Event

    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = search.SearchCapture.append

    def block(capture, encoded):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(capture, encoded)

    monkeypatch.setattr(search.SearchCapture, "append", block)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "取消归档", request_id="cancel")
            )
            try:
                await asyncio.wait_for(entered.wait(), 10)
                turn = (await store.get_thread(thread.thread_id)).turns[-1]
                if kind == "user":
                    await runtime.cancel(thread.thread_id, turn.turn_id)
                else:
                    task.cancel()
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                assert (await store.get_thread(thread.thread_id)).turns[
                    -1
                ].status == TurnStatus.CANCELLED
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["same", "other_thread", "policy", "root", "injected_scope"])
async def test_read_tool_uses_actual_thread_and_rebound_workspace(tmp_path, kind):
    store, artifacts, _, thread, turn = await exercise(tmp_path, count=3)
    ref = results(turn)[0].output["artifact"]["artifact_id"]
    root = tmp_path / "repo"
    if kind == "root":
        root.rename(tmp_path / "old")
        root.mkdir()
    args = {"artifact_id": ref}
    if kind == "injected_scope":
        args["thread_id"] = str(thread.thread_id)
    async with CodingToolRuntime(
        root, artifacts=artifacts, denied_paths=("main.py",) if kind == "policy" else ()
    ) as tools:
        async with AgentRuntime(
            store,
            ScriptedProvider([step("read_artifact", **args), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            if kind == "other_thread":
                thread = await runtime.create_thread(str(tools.workspace_root))
            final = await runtime.run_turn(thread.thread_id, "读取归档", request_id="read")
    result = results(final)[0]
    if kind == "same":
        assert result.outcome == "succeeded" and result.output["artifact"]["records"] == 3
    else:
        assert result.error.code == (
            "tool_invalid_arguments" if kind == "injected_scope" else "artifact_not_found"
        )


@pytest.mark.parametrize("kind", ["task", "user"])
async def test_read_cancellation_drains_before_tool_close(tmp_path, monkeypatch, kind):
    store, artifacts, _, thread, turn = await exercise(tmp_path, count=1)
    ref = results(turn)[0].output["artifact"]["artifact_id"]
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def block(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(artifacts, "read", block)
    async with CodingToolRuntime(tmp_path / "repo", artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            ScriptedProvider([step("read_artifact", artifact_id=ref), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "取消读取", request_id="read")
            )
            close_task = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                close_task = asyncio.create_task(tools.aclose())
                await asyncio.sleep(0)
                assert not close_task.done()
                if kind == "task":
                    task.cancel()
                else:
                    turn = (await store.get_thread(thread.thread_id)).turns[-1]
                    await runtime.cancel(thread.thread_id, turn.turn_id)
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
                await asyncio.wait_for(close_task, 10)
                assert cleaned.is_set() and tools._workspace._root_fd is None
            finally:
                task.cancel()
                await asyncio.gather(
                    task, *([close_task] if close_task else []), return_exceptions=True
                )


@pytest.mark.parametrize("change", ["enable", "disable", "policy"])
async def test_changed_artifact_contract_invalidates_pending_search_approval(tmp_path, change):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    store = SQLiteSessionStore(tmp_path / "s.db")
    original = None if change == "enable" else SQLiteArtifactStore(store)
    async with CodingToolRuntime(root, artifacts=original, require_approval=True) as tools:
        async with AgentRuntime(
            store, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=original
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            waiting = await runtime.run_turn(thread.thread_id, "等待审批", request_id="waiting")
    updated = (
        None
        if change == "disable"
        else SQLiteArtifactStore(
            store, policy=ArtifactPolicy(ttl_seconds=60 if change == "policy" else 86400)
        )
    )
    provider = FakeProvider()
    async with CodingToolRuntime(root, artifacts=updated, require_approval=True) as tools:
        async with AgentRuntime(store, provider, scoped_tools=tools, artifacts=updated) as runtime:
            with pytest.raises(KernelError) as error:
                await reply(runtime, thread.thread_id, waiting)
            assert error.value.code == "tool_contract_changed" and not provider.requests
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("approved", [False, True])
async def test_read_artifact_obeys_approval_before_storage_io(tmp_path, monkeypatch, approved):
    store, artifacts, _, thread, turn = await exercise(tmp_path, count=1)
    ref = results(turn)[0].output["artifact"]["artifact_id"]
    reads = []
    original = artifacts.read

    async def observe(*args, **kwargs):
        reads.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "read", observe)
    async with CodingToolRuntime(
        tmp_path / "repo", artifacts=artifacts, require_approval=True
    ) as tools:
        async with AgentRuntime(
            store,
            ScriptedProvider([step("read_artifact", artifact_id=ref), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            waiting = await runtime.run_turn(thread.thread_id, "审批归档读取", request_id="read")
            assert waiting.status == TurnStatus.WAITING_APPROVAL and not reads
            await reply(runtime, thread.thread_id, waiting, APPROVE if approved else REJECT)
            final = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    assert final.status == TurnStatus.COMPLETED and len(reads) == int(approved)
    result = results(final)[0]
    assert result.outcome == ("succeeded" if approved else "failed")
    if not approved:
        assert result.error.code == "approval_rejected"
