"""未知工具的有界拒绝：结构失败关闭、混合组审批及原预算与取消边界。"""

from __future__ import annotations

from contextlib import ExitStack

import httpx
import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import AgentFailure
from harnessix.agent.models import (
    Budget,
    ItemStatus,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
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
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallRejected
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.models.test_openai_chat import config
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import WireStream, call, chunk, frame, response
from tests.trusted_actions.test_agent_gateway import FakeExecutor, build_gateway, descriptor

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
    """完整结构产生独立拒绝；非法类型、JSON及非对象仍保留原固定诊断。"""
    parts = CallParts(call_id="original-call", name=PRIVATE_NAME, arguments=arguments, type=kind)
    if arguments == '{"value":1}' and kind == "function":
        events = _complete_calls({0: parts}, {tool_alias("test.read"): "test.read"})
        assert events == [ToolCallRejected(call_id="original-call", argument_chars=len(arguments))]
        assert PRIVATE_NAME not in repr(events) and "arguments" not in events[0].model_dump()
        assert "tool" not in events[0].model_dump()
        return
    with pytest.raises(ChatProtocolError) as caught:
        _complete_calls({0: parts}, {tool_alias("test.read"): "test.read"})
    expected = (
        ChatProtocolReason.TOOL_TYPE_INVALID
        if kind != "function"
        else ChatProtocolReason.TOOL_ARGUMENTS_NOT_OBJECT
        if arguments in {"[]", "null"}
        else ChatProtocolReason.TOOL_ARGUMENTS_INVALID
    )
    assert caught.value.reason is expected
    assert str(caught.value) == "Chat终态不符合协议"
    assert PRIVATE_NAME not in str(caught.value)


@pytest.mark.parametrize("unknown_first", [False, True])
@pytest.mark.parametrize("wire_name", [PRIVATE_NAME, "test.read"])
async def test_unknown_group_preserves_registered_write_approval_without_execution(
    tmp_path, unknown_first, wire_name
):
    """合法未知成员闭合拒绝；已登记写成员沿原审批路径等待且尚未执行。"""
    registered = call(0, arguments='{"path":"file.txt"}')
    unknown = call(1, arguments='{"value":"' + PRIVATE_VALUE + '"}')
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

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, _, plans, audit = build_gateway(
        root, executor, tool=descriptor().model_copy(update={"name": "test.read"})
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    with ExitStack() as cleanup:
        cleanup.callback(plans.close)
        cleanup.callback(audit.close)
        async with OpenAIChatProvider(
            config(max_attempts=3, capabilities=ChatCapabilities(parallel_tool_calls=True)),
            transport=httpx.MockTransport(handle),
        ) as provider:
            async with AgentRuntime(store, provider, trusted_actions=gateway) as runtime:
                thread = await runtime.create_thread(str(root))
                turn = await runtime.run_turn(thread.thread_id, "未知工具边界", request_id="r")
    assert turn.status is TurnStatus.WAITING_APPROVAL and turn.error is None, turn.error
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)
    assert len(turn.model_attempts) == 1 and turn.model_attempts[0].status == "completed"
    assert turn.model_attempts[0].error is None and turn.model_steps == 1
    assert len(requests) == 1 and executor.calls == executor.reconciliations == 0 and wire.closed
    assert (root / "file.txt").read_text(encoding="utf-8") == "before"
    proposals = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolCallContent | ToolCallRejectionContent)
    ]
    assert [content.provider_call_id for content in proposals] == [value["id"] for value in calls]
    registered_calls = [content for content in proposals if isinstance(content, ToolCallContent)]
    rejected = [content for content in proposals if isinstance(content, ToolCallRejectionContent)]
    assert len(registered_calls) == len(rejected) == 1
    assert registered_calls[0].tool == "test.read" and registered_calls[0].requires_approval
    assert registered_calls[0].effect_class is EffectClass.NON_IDEMPOTENT_WRITE
    assert rejected[0].reason == "unregistered_tool" and rejected[0].model_step == 1
    assert all(
        item.status is ItemStatus.COMPLETED and item.error is None
        for item in turn.items
        if isinstance(item.content, ToolCallRejectionContent)
    )
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert results == [
        ToolResultContent(
            call_id=rejected[0].call_id,
            outcome="failed",
            error=AgentFailure(code="unknown_tool", message="工具未注册", retryable=False),
        )
    ]
    approvals = [
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    ]
    assert len(approvals) == 1 and approvals[0].call_id == registered_calls[0].call_id
    assert approvals[0].route_state == "pending_approval"
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    events = await reopened.events(thread.thread_id)
    assert replay(events) == restored and restored.turns[-1] == turn
    assert PRIVATE_NAME not in repr(events) and PRIVATE_VALUE not in repr(events)


async def test_normalized_unknown_error_can_be_corrected_only_by_a_new_registered_call(tmp_path):
    """脚本也使用拒绝变体；只有下一步骤的新登记调用可以进入执行。"""
    tools = RecordingTools()
    provider = ScriptedProvider([rejected_step(), tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "纠正未登记调用", request_id="r")
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    rejected = [
        item.content for item in turn.items if isinstance(item.content, ToolCallRejectionContent)
    ]
    assert turn.status is TurnStatus.COMPLETED and turn.model_steps == 3
    assert len(calls) == len(rejected) == 1 and len(results) == 2
    assert rejected[0].call_id != calls[0].call_id
    assert results[0].call_id == rejected[0].call_id
    assert results[0].outcome == "failed" and results[0].error.code == "unknown_tool"
    assert not results[0].error.retryable
    assert results[0].output is None and results[0].error.message == "工具未注册"
    assert results[0].error.category == "tool"
    assert results[1].call_id == calls[0].call_id
    assert results[1].outcome == "succeeded" and tools.calls == [calls[0]]
    feedback = [
        item.content
        for item in provider.requests[1].history
        if isinstance(item.content, ToolResultContent)
    ]
    assert feedback == [results[0]]
    assert any(
        isinstance(item.content, ToolCallRejectionContent) for item in provider.requests[1].history
    )
    assert PRIVATE_NAME not in repr(turn) and PRIVATE_VALUE not in repr(turn)
    assert provider.closed_streams == len(provider.requests) == 3
    reopened = SQLiteSessionStore(store.path)
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("max_steps", [1, 2])
async def test_normalized_unknown_calls_consume_original_step_budget(tmp_path, max_steps):
    provider = ScriptedProvider([rejected_step()] * 3)
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
    rejected = [
        item.content for item in turn.items if isinstance(item.content, ToolCallRejectionContent)
    ]
    assert len(rejected) == max_steps and len({call.call_id for call in rejected}) == max_steps
    assert [call.model_step for call in rejected] == list(range(1, max_steps + 1))
    assert not any(isinstance(item.content, ToolCallContent) for item in turn.items)
    assert len(results) == max_steps and all(
        result.error.code == "unknown_tool" for result in results
    )


async def test_cancel_after_normalized_rejection_does_not_request_correction_or_execute(tmp_path):
    def cancel_after_result(name):
        # 拒绝及固定结果在原子组内形成，复用组提交后的既有检查点。
        if name == "runtime.after_tool_call":
            raise TurnCancelled

    provider = ScriptedProvider([rejected_step(), tool_step("test.read"), answer()])
    tools = RecordingTools()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools, fault=cancel_after_result) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "拒绝后取消", request_id="r")
    assert turn.status is TurnStatus.CANCELLED
    assert len(provider.requests) == provider.closed_streams == 1 and tools.calls == []
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == 1 and results[0].error.code == "unknown_tool"
    rejected = [
        item.content for item in turn.items if isinstance(item.content, ToolCallRejectionContent)
    ]
    assert len(rejected) == 1 and results[0].call_id == rejected[0].call_id
    assert not any(isinstance(item.content, ToolCallContent) for item in turn.items)
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    assert (
        restored.turns[-1] == turn and replay(await reopened.events(thread.thread_id)) == restored
    )


def rejected_step():
    """仅用受控类型表达拒绝，不把未知名称伪装成普通工具调用。"""
    return [
        ResponseStarted(response_id="rejected-response"),
        ToolCallRejected(call_id="unknown-call", argument_chars=2),
        ResponseCompleted(finish_reason="tool_calls"),
    ]
