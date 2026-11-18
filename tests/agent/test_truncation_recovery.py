"""不可用回复的恢复（截断、空回复）：开启后补发提示继续，连续两次后仍按原错误失败；截断的工具调用照旧。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, TextContent, ToolCallContent, TurnStatus, Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.truncation_recovery import (
    EMPTY_REPLY_NOTICE,
    TOOL_CALL_TEXT_NOTICE,
    TRUNCATED_REPLY_NOTICE,
    Reply,
    recovery_notice_item_id,
    tool_call_written_as_text,
)
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_provider_input_budget import WireProvider


def cut(text: str = "Let me reason about every case in detail", output_tokens: int = 64):
    """以输出上限结束、没有工具调用的纯文本步骤。"""
    return [
        ResponseStarted(response_id="cut"),
        TextStarted(content_id="reply"),
        TextDelta(content_id="reply", delta=text),
        TextCompleted(content_id="reply", text=text),
        ResponseCompleted(
            finish_reason="max_output_tokens",
            usage=Usage(input_tokens=10, output_tokens=output_tokens),
        ),
    ]


async def run(tmp_path, provider, *, recover=True, tools=None, budget=None, capped=False):
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        tools,
        recover_truncated_replies=recover,
        budget_capped_truncation=capped,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        return await runtime.run_turn(thread.thread_id, "任务", request_id="cut", budget=budget)


def empty(finish_reason: str = "completed", output_tokens: int = 64):
    """没有正文也没有工具调用的步骤：正常结束（只推理不作答），或推理用满输出上限。"""
    return [
        ResponseStarted(response_id="empty"),
        ResponseCompleted(
            finish_reason=finish_reason,
            usage=Usage(input_tokens=10, output_tokens=output_tokens),
        ),
    ]


def notices(turn, text: str = TRUNCATED_REPLY_NOTICE):
    return [
        item
        for item in turn.items
        if isinstance(item.content, TextContent)
        and item.content.kind == "user_message"
        and item.content.text == text
    ]


async def test_text_truncation_continues_after_a_host_notice(tmp_path):
    provider = ScriptedProvider([cut(), answer("done")])
    turn = await run(tmp_path, provider)
    assert turn.status is TurnStatus.COMPLETED
    (notice,) = notices(turn)
    assert notice.item_id == recovery_notice_item_id(turn.turn_id, 1)
    # 截断的正文作为助手消息保留，补发提示进入下一步的模型历史。
    assert len(provider.requests) == 2
    history = provider.requests[1].history
    assert any(
        isinstance(item.content, TextContent) and item.content.text == TRUNCATED_REPLY_NOTICE
        for item in history
    )


async def test_third_consecutive_truncation_fails_with_the_output_limit(tmp_path):
    turn = await run(tmp_path, ScriptedProvider([cut(), cut(), cut()]))
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "provider_max_output_tokens"
    assert len(notices(turn)) == 2


async def test_a_step_that_ends_normally_resets_the_consecutive_count(tmp_path):
    steps = [cut(), cut(), tool_step("test.read"), cut(), cut(), answer("done")]
    turn = await run(tmp_path, ScriptedProvider(steps), tools=RecordingTools())
    assert turn.status is TurnStatus.COMPLETED
    assert len(notices(turn)) == 4


async def test_recovery_is_off_by_default(tmp_path):
    provider = ScriptedProvider([cut(), answer("done")])
    turn = await run(tmp_path, provider, recover=False)
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "provider_max_output_tokens"
    assert len(provider.requests) == 1


async def test_truncated_tool_calls_still_fail(tmp_path):
    with_call = [
        ResponseStarted(response_id="call"),
        ToolCallCompleted(call_id="read", tool="test.read", arguments={}),
        ResponseCompleted(finish_reason="max_output_tokens"),
    ]
    tools = RecordingTools()
    turn = await run(tmp_path, ScriptedProvider([with_call, answer()]), tools=tools)
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "provider_max_output_tokens"
    assert tools.calls == []
    assert not any(isinstance(item.content, ToolCallContent) for item in turn.items)


async def test_empty_reply_continues_after_a_host_notice(tmp_path):
    """任务做完后模型只推理、不作答就结束：补发提示后给出总结，Turn正常完成。"""
    provider = ScriptedProvider([tool_step("test.read"), empty(), answer("done")])
    turn = await run(tmp_path, provider, tools=RecordingTools())
    assert turn.status is TurnStatus.COMPLETED
    (notice,) = notices(turn, EMPTY_REPLY_NOTICE)
    assert notice.item_id == recovery_notice_item_id(turn.turn_id, 2)
    assert notices(turn) == []
    # 空回复那一步不留下助手消息，用量照常记账；补发提示进入下一步的模型历史。
    assert turn.model_steps == 3 and turn.usage.output_tokens >= 64
    history = provider.requests[2].history
    assert history[-1].content.text == EMPTY_REPLY_NOTICE


async def test_reply_truncated_before_any_text_continues_after_the_truncation_notice(tmp_path):
    """推理用满输出上限、正文一个字没有：按截断补发提示，而不是按空回复。"""
    provider = ScriptedProvider([empty("max_output_tokens"), answer("done")])
    turn = await run(tmp_path, provider)
    assert turn.status is TurnStatus.COMPLETED
    assert len(notices(turn)) == 1 and notices(turn, EMPTY_REPLY_NOTICE) == []


async def test_third_consecutive_empty_reply_fails_as_before(tmp_path):
    turn = await run(tmp_path, ScriptedProvider([empty(), empty(), empty()]))
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "invalid_provider_output"
    assert len(notices(turn, EMPTY_REPLY_NOTICE)) == 2


async def test_truncated_and_empty_replies_share_the_consecutive_count(tmp_path):
    turn = await run(tmp_path, ScriptedProvider([cut(), empty(), cut()]))
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "provider_max_output_tokens"
    assert len(notices(turn)) == 1 and len(notices(turn, EMPTY_REPLY_NOTICE)) == 1


async def test_empty_reply_recovery_is_off_by_default(tmp_path):
    provider = ScriptedProvider([empty(), answer("done")])
    turn = await run(tmp_path, provider, recover=False)
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "invalid_provider_output"
    assert len(provider.requests) == 1


async def test_truncation_at_a_budget_lowered_cap_stays_budget_exhaustion(tmp_path):
    budget = Budget(max_tokens=50_000)
    probe = WireProvider([cut(output_tokens=0), answer()])
    await run(tmp_path / "probe", probe, budget=budget, capped=True)
    cap = probe.requests[0].remaining_tokens
    assert cap < Budget().max_output_chars
    provider = WireProvider([cut(output_tokens=cap), answer()])
    turn = await run(tmp_path, provider, budget=budget, capped=True)
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "budget_exceeded"
    assert notices(turn) == []


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_switch_requires_literal_boolean(tmp_path, value):
    with pytest.raises(KernelError):
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"),
            ScriptedProvider([]),
            recover_truncated_replies=value,
        )


# Beta Issue #40（0.10.0b51 macOS，qwen3.8-flash）：末条回复把工具调用写成了正文，界面显示“已完成”。
CALL_AS_TEXT = (
    "Go is available. Let me reproduce with the exact script from the issue.\n"
    "<previous_tool_call>\n"
    '{"name": "test_read", "arguments": {"cmd": "cd /tmp && go run ."}}\n'
    "</parameter>\n</function>"
)


def said(text: str):
    """正常结束、只有正文的步骤。"""
    return [
        ResponseStarted(response_id="said"),
        TextStarted(content_id="reply"),
        TextDelta(content_id="reply", delta=text),
        TextCompleted(content_id="reply", text=text),
        ResponseCompleted(
            finish_reason="completed", usage=Usage(input_tokens=10, output_tokens=64)
        ),
    ]


async def test_a_tool_call_written_as_text_is_not_a_finished_turn(tmp_path):
    provider = ScriptedProvider([said(CALL_AS_TEXT), tool_step("test.read"), answer("done")])
    tools = RecordingTools()
    turn = await run(tmp_path, provider, tools=tools)
    assert turn.status is TurnStatus.COMPLETED
    (notice,) = notices(turn, TOOL_CALL_TEXT_NOTICE)
    assert notice.item_id == recovery_notice_item_id(turn.turn_id, 1)
    assert len(tools.calls) == 1  # 补发提示之后才真正调用
    assert provider.requests[1].history[-1].content.text == TOOL_CALL_TEXT_NOTICE


async def test_a_tool_call_written_as_text_three_times_fails_instead_of_completing(tmp_path):
    """修改前这里以completed结束。"""
    provider = ScriptedProvider([said(CALL_AS_TEXT)] * 3)
    turn = await run(tmp_path, provider, tools=RecordingTools())
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "invalid_provider_output"
    assert len(notices(turn, TOOL_CALL_TEXT_NOTICE)) == 2


async def test_tool_call_text_recovery_is_off_with_the_other_recoveries(tmp_path):
    turn = await run(tmp_path, ScriptedProvider([said(CALL_AS_TEXT)]), recover=False)
    assert turn.status is TurnStatus.COMPLETED and notices(turn, TOOL_CALL_TEXT_NOTICE) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CALL_AS_TEXT, True),
        ('{"name": "test.read", "arguments": {}}', True),
        ('<tool_call>\n{"name": "test_read", "parameters": {}}\n</tool_call>', True),
        ("<function=test_read>\n<parameter=path>a</parameter>\n</function>", True),
        ('<invoke name="test_read"><parameter name="path">a</parameter></invoke>', True),
        # 正常的总结：提到工具名、贴出代码或JSON，都不算。
        ("I used test_read and grep to check the file; all tests pass.", False),
        ("```html\n<script>function f() {}</script>\n```", False),
        ('Example payload: {"name": "alice", "arguments": []}', False),
        ("The fix wraps <function> in the template; test_read confirmed it.", False),
        ("", False),
    ],
)
def test_what_counts_as_a_tool_call_written_as_text(text: str, expected: bool) -> None:
    assert tool_call_written_as_text(Reply(text, ("test.read",))) is expected
