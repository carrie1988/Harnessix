"""未知工具的现行边界：协议诊断不是结构证明，拒绝不能释放工具或刷新预算。"""

from __future__ import annotations

import httpx
import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.models import (
    ApprovalRequestContent,
    Budget,
    ToolCallContent,
    ToolResultContent,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass
from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason
from harnessix.models._chat_stream import CallParts, _complete_calls
from harnessix.models._history import tool_alias
from harnessix.models.config import ChatCapabilities
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.models.test_openai_chat import config
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import WireStream, call, chunk, frame, response

PRIVATE_NAME = "PRIVATE_UNKNOWN_NAME_NEVER_PUBLISHED"
PRIVATE_VALUE = "PRIVATE_UNKNOWN_ARGUMENT_NEVER_PUBLISHED"


@pytest.mark.parametrize(
    "arguments,kind",
    [
        ('{"value":1}', "function"),
        ("{", "function"),
        ('{"value":1,"value":2}', "function"),
        ('{"value":NaN}', "function"),
        ("[]", "function"),
        ("null", "function"),
        ("{}", None),
    ],
)
def test_unknown_name_diagnostic_alone_does_not_prove_legal_structure(arguments, kind):
    """名称判断早于类型/JSON判断，不能按原固定诊断盲目认定可恢复。"""
    parts = CallParts(call_id="original-call", name=PRIVATE_NAME, arguments=arguments, type=kind)
    with pytest.raises(ChatProtocolError) as caught:
        _complete_calls({0: parts}, {tool_alias("test.read"): "test.read"})
    assert caught.value.reason is ChatProtocolReason.TOOL_NAME_UNKNOWN
    assert str(caught.value) == "Chat终态不符合协议"
    assert PRIVATE_NAME not in str(caught.value)


@pytest.mark.parametrize("unknown_first", [False, True])
@pytest.mark.parametrize("wire_name", [PRIVATE_NAME, "test.read"])
async def test_unknown_group_releases_no_registered_write_or_approval(
    tmp_path, unknown_first, wire_name
):
    """即使同组含已登记写工具，别名外名称也不能部分放行、创建审批或重试。"""
    registered, unknown = call(0), call(1, arguments='{"value":"' + PRIVATE_VALUE + '"}')
    unknown["function"]["name"] = wire_name
    calls = [unknown, registered] if unknown_first else [registered, unknown]
    for index, value in enumerate(calls):
        value["index"] = index
    wire = WireStream(
        [
            frame(chunk({"tool_calls": calls})),
            frame(chunk(finish="tool_calls")),
            frame(chunk(usage=True)),
            b"data: [DONE]\n\n",
        ]
    )
    requests = []

    def handle(request):
        requests.append(request)
        return response(wire)

    tools = RecordingTools(effect=EffectClass.IDEMPOTENT_WRITE, approval=True)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with OpenAIChatProvider(
        config(max_attempts=3, capabilities=ChatCapabilities(parallel_tool_calls=True)),
        transport=httpx.MockTransport(handle),
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "未知工具边界", request_id="r")
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "provider_invalid_provider_output"
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)
    assert len(turn.model_attempts) == 1 and turn.model_attempts[0].status == "failed"
    assert turn.model_attempts[0].error.message.endswith("chat_protocol/v1:tool_name_unknown")
    assert len(requests) == 1 and tools.calls == [] and wire.closed
    assert not any(
        isinstance(item.content, ToolCallContent | ToolResultContent | ApprovalRequestContent)
        for item in turn.items
    )
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    events = await reopened.events(thread.thread_id)
    assert replay(events) == restored and restored.turns[-1] == turn
    assert PRIVATE_NAME not in repr(events) and PRIVATE_VALUE not in repr(events)


async def test_normalized_unknown_error_can_be_corrected_only_by_a_new_registered_call(tmp_path):
    """原Kernel已支持错误反馈；脚本化中立事件不等同于SDK未知名称已可恢复。"""
    tools = RecordingTools()
    provider = ScriptedProvider([tool_step("missing"), tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "纠正未登记调用", request_id="r")
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    assert turn.status is TurnStatus.COMPLETED and turn.model_steps == 3
    assert len(calls) == len(results) == 2
    assert calls[0].call_id != calls[1].call_id
    assert results[0].call_id == calls[0].call_id
    assert results[0].outcome == "failed" and results[0].error.code == "unknown_tool"
    assert not results[0].error.retryable
    assert results[1].outcome == "succeeded" and tools.calls == [calls[1]]
    feedback = [
        item.content
        for item in provider.requests[1].history
        if isinstance(item.content, ToolResultContent)
    ]
    assert feedback == [results[0]]
    assert provider.closed_streams == len(provider.requests) == 3
    reopened = SQLiteSessionStore(store.path)
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("max_steps", [1, 2])
async def test_normalized_unknown_calls_consume_original_step_budget(tmp_path, max_steps):
    provider = ScriptedProvider([tool_step("missing")] * 3)
    tools = RecordingTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "有限错误反馈",
            request_id="r",
            budget=Budget(max_steps=max_steps),
        )
    assert turn.status is TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.model_steps == len(provider.requests) == max_steps
    assert provider.closed_streams == max_steps and tools.calls == []
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == max_steps and all(
        result.error.code == "unknown_tool" for result in results
    )


async def test_cancel_after_normalized_rejection_does_not_request_correction_or_execute(tmp_path):
    def cancel_after_result(name):
        if name == "runtime.after_tool_result":
            raise TurnCancelled

    provider = ScriptedProvider([tool_step("missing"), tool_step("test.read"), answer()])
    tools = RecordingTools()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools, fault=cancel_after_result) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "拒绝后取消", request_id="r")
    assert turn.status is TurnStatus.CANCELLED
    assert len(provider.requests) == provider.closed_streams == 1 and tools.calls == []
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == 1 and results[0].error.code == "unknown_tool"
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    assert (
        restored.turns[-1] == turn and replay(await reopened.events(thread.thread_id)) == restored
    )
