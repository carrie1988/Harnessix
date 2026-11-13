"""空名称分片不覆盖已知名称；最终仍缺名的调用只拒绝该次调用，其他畸形仍不释放工具。"""

import httpx
import pytest

from harnessix.agent.models import ToolCallContent
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.models.test_openai_chat import collect, config
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames, tool_frames


def empty_name_frames(*, deferred=False, registered=True):
    first = call(arguments="{")
    name = first["function"]["name"] if registered else "unregistered-canary"
    first["function"]["name"] = "" if deferred else name
    second = {"index": 0, "function": {"name": name if deferred else "", "arguments": "}"}}
    return [
        frame(chunk({"tool_calls": [first]})),
        frame(chunk({"tool_calls": [second]})),
        *tool_frames()[1:],
    ]


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("registered", [False, True])
async def test_empty_name_is_no_update_not_a_guessed_tool(deferred, registered):
    events, wire = await collect(empty_name_frames(deferred=deferred, registered=registered))
    proposals = [
        event for event in events if isinstance(event, ToolCallCompleted | ToolCallRejected)
    ]
    expected = (
        ToolCallCompleted(call_id=call()["id"], tool="test.read", arguments={})
        if registered
        else ToolCallRejected(call_id=call()["id"], argument_chars=2)
    )
    assert proposals == [expected]
    assert isinstance(events[-1], ResponseCompleted) and events[-1].usage.total_tokens == 12
    terminal = next(event for event in events if isinstance(event, ModelAttemptFinished))
    assert terminal.outcome == "completed" and terminal.error is None
    assert events.index(terminal) < events.index(expected)
    assert "unregistered-canary" not in repr(events)
    assert wire.closed


@pytest.mark.parametrize(
    "violation",
    ["name_changed", "name_too_long", "invalid_type", "missing_done", "missing_usage"],
)
async def test_empty_name_preserves_whole_group_validation_and_no_retry(violation):
    parts = empty_name_frames()
    if violation == "missing_done":
        parts.pop()
    elif violation == "missing_usage":
        parts.pop(-2)
    else:
        update = {"index": 0, "function": {"name": "", "arguments": "}"}}
        if violation == "invalid_type":
            update["type"] = ""
        else:
            update["function"]["name"] = "changed" if violation == "name_changed" else "X" * 257
        parts[1] = frame(chunk({"tool_calls": [update]}))
    events, wire = await collect(parts, max_attempts=3)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(
        isinstance(event, ToolCallCompleted | ToolCallRejected | ResponseCompleted)
        for event in events
    )
    assert sum(isinstance(event, ModelAttemptStarted) for event in events) == 1
    assert wire.closed


@pytest.mark.parametrize("nameless_index", [0, 1])
async def test_a_call_whose_name_never_arrives_is_rejected_alone(nameless_index):
    """整流结束仍缺名的调用只拒绝这一次，同组里名称完整的调用照常放行（ADR 0157）。

    0.10.0b24 Beta（Issue #1）：这样的响应此前让整个请求以tool_name_unknown失败，任务中断。
    """
    parts = empty_name_frames()
    nameless = call(index=nameless_index, arguments="{" if nameless_index == 0 else "{}")
    nameless["function"]["name"] = ""
    if nameless_index == 0:
        parts[0] = frame(chunk({"tool_calls": [nameless]}))
    else:
        parts.insert(2, frame(chunk({"tool_calls": [nameless]})))
    events, wire = await collect(parts, max_attempts=3)
    proposals = [
        event for event in events if isinstance(event, ToolCallCompleted | ToolCallRejected)
    ]
    rejected = ToolCallRejected(call_id=call(index=nameless_index)["id"], argument_chars=2)
    completed = ToolCallCompleted(call_id=call()["id"], tool="test.read", arguments={})
    assert proposals == ([rejected] if nameless_index == 0 else [completed, rejected])
    assert isinstance(events[-1], ResponseCompleted) and events[-1].finish_reason == "tool_calls"
    assert sum(isinstance(event, ModelAttemptStarted) for event in events) == 1
    assert wire.closed


async def test_empty_name_keeps_actual_runtime_execution_and_replay(tmp_path):
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    tools = RecordingTools()
    wires = [WireStream(empty_name_frames()), WireStream(text_frames())]
    requests = []

    def handle(request):
        requests.append(request)
        return response(wires[len(requests) - 1])

    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "合成空名称增量", request_id="r")
    assert turn.status == "completed" and turn.model_steps == 2
    assert len(requests) == 2 and all(wire.closed for wire in wires)
    calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    assert len(calls) == 1 and calls[0].tool == "test.read" and calls[0].arguments == {}
    assert tools.calls == calls
    reopened = SQLiteSessionStore(tmp_path / "session.sqlite")
    restored = await reopened.get_thread(thread.thread_id)
    assert restored.turns[-1] == turn
    assert replay(await reopened.events(thread.thread_id)) == restored


async def test_empty_name_with_malformed_arguments_rejects_only_that_call():
    """后续分片名称为空是合法续传；参数最终不是JSON对象时只拒绝这一次调用（ADR 0109第5版）。"""
    parts = empty_name_frames()
    update = {"index": 0, "function": {"name": "", "arguments": "bad-json}"}}
    parts[1] = frame(chunk({"tool_calls": [update]}))
    events, wire = await collect(parts, max_attempts=3)
    rejected = [event for event in events if isinstance(event, ToolCallRejected)]
    assert [event.reason for event in rejected] == ["invalid_arguments_json"]
    assert not any(isinstance(event, ToolCallCompleted) for event in events)
    assert isinstance(events[-1], ResponseCompleted) and events[-1].finish_reason == "tool_calls"
    assert sum(isinstance(event, ModelAttemptStarted) for event in events) == 1
    assert wire.closed
