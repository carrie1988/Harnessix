from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ThreadArchived,
    ThreadForked,
    ToolCallContent,
    ToolResultContent,
    TurnStarted,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.artifacts.helpers import exercise
from tests.helpers import RecordingObservability


async def test_resume_and_archive_are_durable_and_archive_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    observer = RecordingObservability()
    async with AgentRuntime(store, FakeProvider(), observability=observer) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "完成任务", request_id="turn-1")
        resumed = await runtime.resume_thread(thread.thread_id)
        archived = await runtime.archive_thread(thread.thread_id, reason="用户整理")
        assert await runtime.archive_thread(thread.thread_id, reason="用户整理") == archived
        with pytest.raises(KernelError) as error:
            await runtime.archive_thread(thread.thread_id)
        assert error.value.code == "thread_archive_conflict"

        assert resumed.archive is None
        assert archived.archive is not None
        assert archived.archive.reason == "用户整理"
        with pytest.raises(KernelError) as error:
            await runtime.resume_thread(thread.thread_id)
        assert error.value.code == "thread_archived"
        with pytest.raises(KernelError) as error:
            await runtime.run_turn(thread.thread_id, "继续", request_id="turn-2")
        assert error.value.code == "thread_archived"
        with pytest.raises(KernelError) as error:
            await runtime.fork_thread(thread.thread_id, request_id="fork-1")
        assert error.value.code == "thread_archived"

    events = await store.events(thread.thread_id)
    assert sum(isinstance(event.payload, ThreadArchived) for event in events) == 1
    assert replay(events) == archived == await store.get_thread(thread.thread_id)
    lifecycle = [metric for metric in observer.metrics if metric[1].endswith("thread.lifecycle")]
    assert [metric[3] for metric in lifecycle] == [
        {"action": "resume", "outcome": "completed"},
        {"action": "archive", "outcome": "completed"},
        {"action": "archive", "outcome": "idempotent"},
        {"action": "archive", "outcome": "rejected"},
        {"action": "resume", "outcome": "rejected"},
        {"action": "fork", "outcome": "rejected"},
    ]


async def test_fork_inherits_history_without_replaying_completed_tool_effects(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    source_tools = RecordingTools()
    source_provider = ScriptedProvider([tool_step("test.read"), answer("源任务完成")])
    async with AgentRuntime(store, source_provider, source_tools) as runtime:
        source = await runtime.create_thread(str(tmp_path))
        source_turn = await runtime.run_turn(source.thread_id, "读取并分析", request_id="source")
    assert len(source_tools.calls) == 1

    child_provider = FakeProvider("分支继续完成")
    child_tools = RecordingTools()
    async with AgentRuntime(store, child_provider, child_tools) as runtime:
        child = await runtime.fork_thread(source.thread_id, request_id="fork-safe")
        repeated = await runtime.fork_thread(source.thread_id, request_id="fork-safe")
        assert repeated == child
        child_turn = await runtime.run_turn(child.thread_id, "继续分支", request_id="child")

    assert child.fork_snapshot is not None
    assert child.fork_snapshot.authority == "none"
    assert child.fork_snapshot.source_thread_id == source.thread_id
    assert child.fork_snapshot.through_turn_id == source_turn.turn_id
    assert child.turns == ()
    assert len(child.fork_snapshot.tool_result_view_decisions) == 1
    assert child_tools.calls == []
    request = child_provider.requests[0]
    assert sum(isinstance(item.content, ToolCallContent) for item in request.history) == 1
    assert sum(isinstance(item.content, ToolResultContent) for item in request.history) == 1
    assert request.history[-1].content.kind == "user_message"
    assert child_turn.status == "completed"
    assert len(await store.events(child.thread_id)) > 1
    assert isinstance((await store.events(child.thread_id))[0].payload, ThreadForked)
    assert await store.rebuild(child.thread_id) == await store.get_thread(child.thread_id)


async def test_fork_through_terminal_turn_excludes_later_history(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with AgentRuntime(store, FakeProvider("第一答复")) as runtime:
        source = await runtime.create_thread(str(tmp_path))
        first = await runtime.run_turn(source.thread_id, "第一问题", request_id="first")
        await runtime.run_turn(source.thread_id, "第二问题", request_id="second")
        child = await runtime.fork_thread(
            source.thread_id,
            request_id="fork-first",
            through_turn_id=first.turn_id,
        )

    assert child.fork_snapshot is not None
    texts = [
        item.content.text
        for item in child.fork_snapshot.items
        if item.content.kind in {"user_message", "assistant_message"}
    ]
    assert texts == ["第一问题", "第一答复"]


async def test_nested_fork_keeps_original_artifact_owner_and_inherited_history(
    tmp_path: Path,
) -> None:
    store, _, _, source, _ = await exercise(tmp_path, count=300)
    artifacts = SQLiteArtifactStore(store)
    root = tmp_path / "repo"
    provider = FakeProvider("分支读取历史成功")
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            child = await runtime.fork_thread(source.thread_id, request_id="artifact-fork")
            grandchild = await runtime.fork_thread(child.thread_id, request_id="nested-fork")
            await runtime.run_turn(grandchild.thread_id, "继续", request_id="grandchild")

    assert child.fork_snapshot is not None
    assert grandchild.fork_snapshot is not None
    assert grandchild.fork_snapshot.items == child.fork_snapshot.items
    assert grandchild.fork_snapshot.artifact_owners
    assert {owner.owner_thread_id for owner in grandchild.fork_snapshot.artifact_owners} == {
        source.thread_id
    }
    assert await store.rebuild(grandchild.thread_id) == await store.get_thread(grandchild.thread_id)
    assert provider.requests


async def test_archive_rejects_active_turn_and_fork_request_conflict(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        source = await runtime.create_thread(str(tmp_path))
        await store.append(
            source.thread_id,
            [
                EventDraft(
                    turn_id=new_id(),
                    payload=TurnStarted(
                        request_id="active-turn",
                        request_fingerprint="0" * 64,
                        budget=Budget(),
                    ),
                )
            ],
            expected_sequence=source.sequence,
        )
        with pytest.raises(KernelError) as error:
            await runtime.archive_thread(source.thread_id)
        assert error.value.code == "thread_busy"

    store = SQLiteSessionStore(tmp_path / "fork-session.sqlite")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        source = await runtime.create_thread(str(tmp_path))
        with pytest.raises(KernelError) as error:
            await runtime.fork_thread(source.thread_id, request_id="")
        assert error.value.code == "thread_fork_invalid"
        first = await runtime.fork_thread(source.thread_id, request_id="same")
        await runtime.run_turn(source.thread_id, "推进来源", request_id="source-turn")
        with pytest.raises(KernelError) as error:
            await runtime.fork_thread(source.thread_id, request_id="same")
        assert error.value.code == "event_conflict"
        assert await store.get_thread(first.thread_id) == first
