"""工具形态的正文不是执行来源；原生字段与持久化事实必须严格区分。"""

from __future__ import annotations

import httpx
import pytest

from harnessix.agent.models import TextContent, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models._history import tool_alias
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    TextCompleted,
    ToolCallCompleted,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.models.test_openai_chat import collect, config
from tests.models.wire import WireStream, call, chunk, frame, response

ALIAS = tool_alias("test.read")
MARKUP = (
    f"<tool_call>\n<function={ALIAS}>\n"
    "<parameter=path>fixture.py</parameter>\n</function>\n</tool_call>"
)
TEXT_CASES = [
    pytest.param(MARKUP, id="complete-markup"),
    pytest.param(MARKUP.removeprefix("<tool_call>\n"), id="missing-opening-tag"),
    pytest.param(MARKUP[: len(MARKUP) // 2], id="partial-markup"),
    pytest.param(f"```xml\n{MARKUP}\n```", id="quoted-example"),
    pytest.param('{"tool_calls":[{"function":{"name":"test.read"}}]}', id="json-text"),
]


@pytest.fixture(autouse=True)
def fixture_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESSIX_TEST_KEY", "text-boundary-fixture-only")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)


def text_parts(text: str, *, finish: str = "stop") -> list[bytes]:
    """故意跨标签切分，防止后继实现把流式拼接误当执行协议。"""
    cuts = (text[:7], text[7:19], text[19:])
    return [
        *(frame(chunk({"content": part})) for part in cuts if part),
        frame(chunk(finish=finish)),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]


@pytest.mark.parametrize("text", TEXT_CASES)
async def test_tool_shaped_text_remains_text_without_native_calls(text: str) -> None:
    events, wire = await collect(text_parts(text), max_attempts=3)
    assert [event.text for event in events if isinstance(event, TextCompleted)] == [text]
    assert not any(isinstance(event, ToolCallCompleted) for event in events)
    assert events[-1] == ResponseCompleted(
        finish_reason="completed", usage=Usage(input_tokens=10, output_tokens=2)
    )
    assert [event.outcome for event in events if isinstance(event, ModelAttemptFinished)] == [
        "completed"
    ]
    assert wire.closed


@pytest.mark.parametrize("text", TEXT_CASES)
async def test_text_cannot_satisfy_native_tool_finish_contract(text: str) -> None:
    events, wire = await collect(text_parts(text, finish="tool_calls"), max_attempts=3)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(event, ToolCallCompleted | ResponseCompleted) for event in events)
    attempts = [event for event in events if isinstance(event, ModelAttemptFinished)]
    assert len(attempts) == 1
    assert attempts[0].error.message.endswith("chat_protocol/v1:finish_tool_mismatch")
    assert not attempts[0].error.retryable and wire.closed


async def test_native_call_and_matching_text_release_only_the_native_call() -> None:
    parts = text_parts(MARKUP, finish="tool_calls")
    parts.insert(0, frame(chunk({"tool_calls": [call(arguments='{"path":"fixture.py"}')]})))
    events, wire = await collect(parts)
    calls = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert len(calls) == 1
    assert calls[0].tool == "test.read" and calls[0].arguments == {"path": "fixture.py"}
    assert [event.text for event in events if isinstance(event, TextCompleted)] == [MARKUP]
    assert events[-1].finish_reason == "tool_calls" and wire.closed


@pytest.mark.parametrize("text", TEXT_CASES[:2])
async def test_completed_text_turn_reopens_without_creating_tool_effects(
    tmp_path, text: str
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    wire = WireStream(text_parts(text))
    tools = RecordingTools()
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(wire)

    async with OpenAIChatProvider(
        config(max_attempts=3), transport=httpx.MockTransport(handle)
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "验证正文边界", request_id="text-only")
    assert turn.status == "completed" and turn.error is None
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)
    assert tools.calls == [] and len(requests) == 1 and wire.closed
    restored_store = SQLiteSessionStore(store.path)
    restored = await restored_store.get_thread(thread.thread_id)
    events = await restored_store.events(thread.thread_id)
    assert restored.turns[-1] == turn and replay(events) == restored
    assistant_texts = [
        item.content.text
        for item in turn.items
        if isinstance(item.content, TextContent) and item.content.kind == "assistant_message"
    ]
    assert assistant_texts == [text]
