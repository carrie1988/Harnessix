"""空名称分片不覆盖已知名称；最终缺名及其他畸形仍不释放工具。"""

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
    [
        "missing_name",
        "name_changed",
        "name_too_long",
        "invalid_type",
        "invalid_arguments",
        "missing_done",
        "missing_usage",
        "second_call_missing_name",
    ],
)
async def test_empty_name_preserves_whole_group_validation_and_no_retry(violation):
    parts = empty_name_frames()
    if violation == "missing_name":
        first = call(arguments="{")
        first["function"]["name"] = ""
        parts[0] = frame(chunk({"tool_calls": [first]}))
    elif violation == "second_call_missing_name":
        second = call(index=1)
        second["function"]["name"] = ""
        parts.insert(2, frame(chunk({"tool_calls": [second]})))
    elif violation == "missing_done":
        parts.pop()
    elif violation == "missing_usage":
        parts.pop(-2)
    else:
        update = {"index": 0, "function": {"name": "", "arguments": "}"}}
        if violation == "invalid_type":
            update["type"] = ""
        elif violation == "invalid_arguments":
            update["function"]["arguments"] = "bad-json}"
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
