"""Fork v2只继承闭合只读拒绝历史；旧v1事件与请求身份保持原样。"""

from __future__ import annotations

from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from harnessix.agent.lifecycle import prepare_fork_snapshot, validate_fork_snapshot
from harnessix.agent.models import (
    EventDraft,
    ThreadForked,
    ThreadForkSnapshot,
    ThreadForkSnapshotV2,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import prepare_model_history
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_tool_rejection_runtime import rejected_step


async def source_with_rejection(tmp_path):
    store = SQLiteSessionStore(tmp_path / "fork.db")
    async with AgentRuntime(store, ScriptedProvider([rejected_step(), answer()])) as runtime:
        source = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(source.thread_id, "纠正目录调用", request_id="r")
    assert turn.status == TurnStatus.COMPLETED
    return store, await store.get_thread(source.thread_id)


async def test_fork_preserves_rejection_and_original_owners_without_execution(tmp_path):
    store, source = await source_with_rejection(tmp_path)
    tools = RecordingTools()
    provider = ScriptedProvider([answer()])
    async with AgentRuntime(store, provider, tools) as runtime:
        child = await runtime.fork_thread(source.thread_id, request_id="fork")
        assert child.thread_id == uuid5(source.thread_id, "harnessix.thread-fork/v1:fork")
        assert isinstance(child.fork_snapshot, ThreadForkSnapshotV2)
        snapshot = child.fork_snapshot
        assert snapshot.authority == "none" and not child.turns
        assert (
            len([i for i in snapshot.items if isinstance(i.content, ToolCallRejectionContent)]) == 1
        )
        assert not any(isinstance(i.content, ToolCallContent) for i in snapshot.items)
        assert validate_fork_snapshot(source, snapshot) is not None
        assert await runtime.fork_thread(source.thread_id, request_id="fork") == child
        turn = await runtime.run_turn(child.thread_id, "读取继承历史", request_id="child")
        assert turn.status == TurnStatus.COMPLETED and tools.calls == []
    history = provider.requests[0].history
    assert any(isinstance(i.content, ToolCallRejectionContent) for i in history)
    assert any(isinstance(i.content, ToolResultContent) for i in history)
    reopened = SQLiteSessionStore(store.path)
    current = await reopened.get_thread(child.thread_id)
    assert replay(await reopened.events(child.thread_id)) == current
    async with AgentRuntime(reopened, FakeProvider()) as runtime:
        nested = await runtime.fork_thread(child.thread_id, request_id="nested")
    assert isinstance(nested.fork_snapshot, ThreadForkSnapshotV2)
    assert nested.fork_snapshot.artifact_owners == snapshot.artifact_owners


@pytest.mark.parametrize("version", range(1, 21))
@pytest.mark.parametrize("empty", [False, True])
async def test_v2_even_empty_snapshot_cannot_be_disguised_as_old_event(tmp_path, version, empty):
    store, source = await source_with_rejection(tmp_path)
    if empty:
        async with AgentRuntime(store, FakeProvider()) as runtime:
            source = await runtime.create_thread(str(tmp_path))
    snapshot = prepare_fork_snapshot(
        source, request_id="f", through_turn_id=None, policy=ToolResultViewPolicy()
    ).snapshot
    assert isinstance(snapshot, ThreadForkSnapshotV2)
    with pytest.raises(ValidationError, match="v21"):
        EventDraft(
            schema_version=version,
            payload=ThreadForked(workspace=source.workspace, snapshot=snapshot),
        )


async def test_v1_rejects_new_nested_history_even_when_outer_event_is_new(tmp_path):
    _, source = await source_with_rejection(tmp_path)
    snapshot = prepare_fork_snapshot(
        source, request_id="f", through_turn_id=None, policy=ToolResultViewPolicy()
    ).snapshot
    raw = snapshot.model_dump(mode="json")
    raw["spec_version"] = "harnessix.thread-fork/v1"
    with pytest.raises(ValidationError, match="Fork只能"):
        ThreadForkSnapshot.model_validate(raw)
    with pytest.raises(ValidationError):
        EventDraft(
            schema_version=21,
            payload={"type": "thread_forked", "workspace": source.workspace, "snapshot": raw},
        )


async def test_smallest_formal_view_policy_keeps_rejection_result_fixed(tmp_path):
    _, source = await source_with_rejection(tmp_path)
    from harnessix.agent.tool_rejections import rejection_result

    prepared = prepare_model_history(source, 1, ToolResultViewPolicy(max_inline_utf8_bytes=1024))
    rejected = next(
        i.content for i in prepared.history if isinstance(i.content, ToolCallRejectionContent)
    )
    result = next(i.content for i in prepared.history if isinstance(i.content, ToolResultContent))
    assert result == rejection_result(rejected.call_id)
    assert not prepared.references
    decisions = source.turns[-1].tool_result_view_decisions
    assert len(decisions) == 1
    assert decisions[0].strategy == "inline" and decisions[0].replacement_output is None


@pytest.mark.parametrize("tamper", ["orphan", "success", "output", "call_id", "result_error"])
async def test_fork_v2_requires_fixed_non_effectful_results(tmp_path, tamper):
    _, source = await source_with_rejection(tmp_path)
    snapshot = prepare_fork_snapshot(
        source, request_id="f", through_turn_id=None, policy=ToolResultViewPolicy()
    ).snapshot
    raw = snapshot.model_dump(mode="json")
    result = next(i for i in raw["items"] if i["content"]["kind"] == "tool_result")
    if tamper == "orphan":
        raw["items"].remove(result)
    elif tamper == "success":
        result["content"]["outcome"] = "succeeded"
    elif tamper == "output":
        result["content"]["output"] = {"forged": True}
    elif tamper == "call_id":
        result["content"]["call_id"] = str(uuid4())
    else:
        result["error"] = {
            "code": "forged",
            "message": "非法外层错误",
            "category": "tool",
            "retryable": False,
        }
    with pytest.raises(ValidationError):
        ThreadForkSnapshotV2.model_validate(raw)


async def test_old_v1_creation_retry_preserves_stored_event_and_same_child(tmp_path):
    store = SQLiteSessionStore(tmp_path / "old.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        source = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(source.thread_id, "旧版完成任务", request_id="old-turn")
        source = await store.get_thread(source.thread_id)
        prepared = prepare_fork_snapshot(
            source,
            request_id="same",
            through_turn_id=None,
            policy=ToolResultViewPolicy(),
            spec_version="harnessix.thread-fork/v1",
        )
        snapshot = prepared.snapshot
        assert isinstance(snapshot, ThreadForkSnapshot)
        child_id = uuid5(source.thread_id, "harnessix.thread-fork/v1:same")
        old_draft = EventDraft(
            schema_version=20,
            event_id=uuid5(child_id, "harnessix.thread-fork-event/v1"),
            occurred_at=source.updated_at,
            payload=ThreadForked(workspace=source.workspace, snapshot=snapshot),
        )
        child = await store.fork(
            source.thread_id, child_id, old_draft, expected_source_sequence=source.sequence
        )
        before = await store.events(child_id)
        assert validate_fork_snapshot(source, snapshot) is not None
        assert await runtime.fork_thread(source.thread_id, request_id="same") == child
        assert await store.events(child_id) == before
        assert before[0].schema_version == 20
        assert isinstance((await store.get_thread(child_id)).fork_snapshot, ThreadForkSnapshot)
