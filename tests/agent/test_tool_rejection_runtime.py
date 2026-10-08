"""不可执行拒绝的真实Kernel、Session及恢复边界；不调用真实模型。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemStarted,
    Thread,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_rejections import require_closed_rejections
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


def rejected_step(chars=2):
    return [
        ResponseStarted(response_id="response"),
        ToolCallRejected(call_id="rejected", argument_chars=chars),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def test_rejection_then_new_registered_call_is_persistent_and_non_executable(tmp_path):
    tools = RecordingTools()
    provider = ScriptedProvider([rejected_step(), tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "完成编码任务", request_id="r")
    rejects = [i.content for i in turn.items if isinstance(i.content, ToolCallRejectionContent)]
    calls = [i.content for i in turn.items if isinstance(i.content, ToolCallContent)]
    assert turn.status == TurnStatus.COMPLETED and turn.model_steps == 3
    assert len(rejects) == len(calls) == len(tools.calls) == 1
    assert rejects[0].call_id != calls[0].call_id and rejects[0].model_step == 1
    assert tools.calls == calls
    results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert results[0].call_id == rejects[0].call_id and results[0].error.code == "unknown_tool"
    assert results[0].outcome == "failed" and not results[0].error.retryable
    assert any(
        isinstance(i.content, ToolCallRejectionContent) for i in provider.requests[1].history
    )
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    assert replay(await reopened.events(thread.thread_id)) == restored
    require_closed_rejections(restored)
    assert all(event.schema_version == 21 for event in await reopened.events(thread.thread_id))


@pytest.mark.parametrize("unknown_first", [False, True])
async def test_mixed_group_has_all_calls_before_rejection_results_and_executes_only_known(
    tmp_path, unknown_first
):
    events = [
        ToolCallCompleted(call_id="known", tool="test.read"),
        ToolCallRejected(call_id="unknown", argument_chars=2),
    ]
    if unknown_first:
        events.reverse()
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="response"),
                *events,
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer(),
        ]
    )
    tools = RecordingTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "混合提案", request_id="r")
    assert turn.status == TurnStatus.COMPLETED and len(tools.calls) == 1
    group = [
        i.content
        for i in turn.items
        if isinstance(i.content, ToolCallContent | ToolCallRejectionContent | ToolResultContent)
    ]
    assert all(isinstance(i, ToolCallContent | ToolCallRejectionContent) for i in group[:2])
    assert all(isinstance(i, ToolResultContent) for i in group[2:])
    assert len(group) == 4


@pytest.mark.parametrize("limit", [1, 2])
async def test_rejections_use_original_step_budget(tmp_path, limit):
    provider = ScriptedProvider([rejected_step()] * 3)
    tools = RecordingTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "有限纠正", request_id="r", budget=Budget(max_steps=limit)
        )
    assert turn.status == TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.model_steps == len(provider.requests) == limit and tools.calls == []


async def test_erasing_arguments_does_not_erase_output_budget(tmp_path):
    provider = ScriptedProvider([rejected_step(chars=1000), answer()])
    tools = RecordingTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "参数预算", request_id="r", budget=Budget(max_output_chars=500)
        )
    assert turn.status == TurnStatus.FAILED and len(provider.requests) == 1 and tools.calls == []
    assert not any(isinstance(i.content, ToolCallRejectionContent) for i in turn.items)


@pytest.mark.parametrize("tail", ["duplicate", "after-terminal", "incomplete"])
async def test_invalid_step_commits_no_partial_proposal_group(tmp_path, tail):
    calls = [ToolCallCompleted(call_id="same", tool="test.read")]
    if tail == "duplicate":
        calls.append(ToolCallRejected(call_id="same", argument_chars=0))
    events = [ResponseStarted(response_id="response"), *calls]
    if tail != "incomplete":
        events.append(ResponseCompleted(finish_reason="tool_calls"))
    if tail == "after-terminal":
        events.append(ToolCallRejected(call_id="late", argument_chars=0))
    provider = ScriptedProvider([events])
    tools = RecordingTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "不完整提案", request_id="r")
    assert turn.status == TurnStatus.FAILED and tools.calls == []
    assert not any(
        isinstance(i.content, ToolCallContent | ToolCallRejectionContent) for i in turn.items
    )


@pytest.mark.parametrize("version", range(1, 21))
@pytest.mark.parametrize("reason_only", [False, True])
def test_new_semantics_cannot_be_serialized_as_old_event(version, reason_only):
    payload = (
        TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT, reason="tool_rejection")
        if reason_only
        else ItemStarted(
            item_id=uuid4(),
            content=ToolCallRejectionContent(
                call_id=uuid4(), provider_call_id="call", model_step=1
            ),
        )
    )
    with pytest.raises(ValidationError):
        EventDraft(schema_version=version, payload=payload)


@pytest.mark.parametrize("chars", [True, "1", -1, 1_000_001, None])
def test_provider_rejection_character_count_is_strict_and_bounded(chars):
    with pytest.raises(ValidationError):
        TypeAdapter(ProviderEvent).validate_python(
            {"type": "tool_call_rejected", "call_id": "call", "argument_chars": chars}
        )


async def test_persisted_snapshot_rejects_orphan_and_forged_rejection_result(tmp_path):
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, ScriptedProvider([rejected_step(), answer()])) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "闭合历史", request_id="r")
    thread = await store.get_thread(thread.thread_id)
    reject = next(
        i for i in thread.turns[0].items if isinstance(i.content, ToolCallRejectionContent)
    )
    for operation in ["remove-result", "wrong-result", "result-before-fact"]:
        data = thread.model_dump(mode="json")
        items = data["turns"][0]["items"]
        idx = next(j for j, i in enumerate(items) if i["content"]["kind"] == "tool_result")
        if operation == "remove-result":
            items.pop(idx)
        elif operation == "wrong-result":
            items[idx]["content"]["output"] = {"not_a_rejection": True}
        else:
            items.insert(0, items.pop(idx))
        with pytest.raises(ValidationError):
            Thread.model_validate(data)
    with pytest.raises(KernelError):
        require_closed_rejections(
            thread.model_copy(
                update={"turns": (thread.turns[0].model_copy(update={"items": (reject,)}),)}
            )
        )
