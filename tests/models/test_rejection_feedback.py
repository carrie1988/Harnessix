"""目录拒绝在模型线协议上的英文反馈：持久固定结果与界面不变，历史正文不随目录变化。"""

from __future__ import annotations

import json
from typing import Any, Literal, get_args
from uuid import uuid4

import pytest

from harnessix.agent.models import (
    Item,
    ItemStatus,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_rejections import RejectionReason, rejection_feedback, rejection_result
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models._history import tool_alias
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ResponseStarted,
    ToolCallRejected,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.contracts.provider import model_request

Kind = Literal["chat", "anthropic"]
REASONS: tuple[RejectionReason, ...] = get_args(RejectionReason)
SENTINEL = "harnessix_rejected_tool_v1"


def build(kind: Kind, request: ModelRequest) -> dict[str, Any]:
    if kind == "chat":
        return chat_request(request, OpenAIChatConfig(model="test-model"))[0]
    return anthropic_request(request, AnthropicConfig(model="test-model"))[0]


def tool_results(kind: Kind, body: dict[str, Any]) -> list[dict[str, Any]]:
    if kind == "chat":
        return [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
    return [
        json.loads(block["content"])
        for message in body["messages"]
        if message["role"] == "user"
        for block in message["content"]
        if block["type"] == "tool_result"
    ]


def rejected_history(reason: RejectionReason) -> tuple[Item, ...]:
    rejection = ToolCallRejectionContent(
        call_id=uuid4(), provider_call_id="provider-id", model_step=1, reason=reason
    )
    return tuple(
        Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)
        for content in (rejection, rejection_result(rejection.call_id, reason))
    )


@pytest.mark.parametrize("reason", REASONS)
def test_feedback_is_english_and_says_nothing_ran_and_what_to_send_next(
    reason: RejectionReason,
) -> None:
    feedback = rejection_feedback(reason)
    assert feedback.isascii()
    assert "nothing was executed" in feedback and "Send the call again" in feedback
    persisted = rejection_result(uuid4(), reason)
    assert persisted.error is not None and persisted.error.message != feedback


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
@pytest.mark.parametrize("reason", REASONS)
def test_wire_result_carries_the_feedback_and_persisted_item_keeps_the_fixed_result(
    kind: Kind, reason: RejectionReason
) -> None:
    request = model_request(with_tools=True)
    history = rejected_history(reason)
    request = request.model_copy(update={"history": (*request.history, *history)})
    before = request.model_dump_json()
    (result,) = tool_results(kind, build(kind, request))
    fixed = rejection_result(history[0].content.call_id, reason)
    assert fixed.error is not None
    assert result == {
        "outcome": "failed",
        "output": None,
        "error": {
            "code": fixed.error.code,
            "message": rejection_feedback(reason),
            "retryable": False,
            "category": "tool",
        },
    }
    assert request.model_dump_json() == before and history[1].content == fixed


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
def test_feedback_does_not_depend_on_the_catalog_so_history_bytes_stay_stable(kind: Kind) -> None:
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={"history": (*request.history, *rejected_history("unregistered_tool"))}
    )
    renamed = request.model_copy(
        update={"tools": (request.tools[0].model_copy(update={"name": "other.tool"}),)}
    )
    without_tools = request.model_copy(update={"tools": ()})
    results = [tool_results(kind, build(kind, each)) for each in (request, renamed, without_tools)]
    assert results[0] == results[1] == results[2]
    assert "test_read" not in json.dumps(results[0]) and "other_tool" not in json.dumps(results[0])


async def test_unregistered_call_is_answered_in_english_next_to_the_callable_names(tmp_path):
    """评测中45次未注册调用只得到“工具未注册”；模型看不出自己的调用已被替换，也不知道该用哪个名字。"""

    class ShellTools(RecordingTools):
        def definitions(self):
            return tuple(
                definition.model_copy(update={"name": "exec_command.testbed"})
                for definition in super().definitions()
            )

    rejected_step = [
        ResponseStarted(response_id="response"),
        ToolCallRejected(call_id="mistyped", argument_chars=40),
        ResponseCompleted(finish_reason="tool_calls"),
    ]
    provider = ScriptedProvider([rejected_step, tool_step("exec_command.testbed"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    tools = ShellTools()
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "run the tests", request_id="r")
    assert turn.status is TurnStatus.COMPLETED and len(tools.calls) == 1
    # 持久事实（界面与回放读取的内容）仍是固定结果。
    persisted = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert persisted[0].error is not None and persisted[0].error.message == "工具未注册"
    body = build("chat", provider.requests[1])
    call = body["messages"][2]["tool_calls"][0]["function"]
    assert call == {"name": SENTINEL, "arguments": "{}"}
    (result,) = tool_results("chat", body)
    assert result["error"]["message"] == rejection_feedback("unregistered_tool")
    # 同一请求的目录说明列出可调用的名字，与工具定义里的别名相同。
    alias = tool_alias("exec_command.testbed")
    note = body["messages"][0]["content"]
    assert f"Current request tool catalog (wire aliases): {json.dumps([alias])}" in note
    assert [tool["function"]["name"] for tool in body["tools"]] == [alias]
