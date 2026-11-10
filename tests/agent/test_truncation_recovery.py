"""纯文本截断恢复：开启后补发提示继续，连续两次后仍按输出上限失败；其他截断照旧。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, TextContent, ToolCallContent, TurnStatus, Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.truncation_recovery import (
    TRUNCATED_REPLY_NOTICE,
    truncation_notice_item_id,
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


def notices(turn):
    return [
        item
        for item in turn.items
        if isinstance(item.content, TextContent)
        and item.content.kind == "user_message"
        and item.content.text == TRUNCATED_REPLY_NOTICE
    ]


async def test_text_truncation_continues_after_a_host_notice(tmp_path):
    provider = ScriptedProvider([cut(), answer("done")])
    turn = await run(tmp_path, provider)
    assert turn.status is TurnStatus.COMPLETED
    (notice,) = notices(turn)
    assert notice.item_id == truncation_notice_item_id(turn.turn_id, 1)
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


async def test_truncated_tool_calls_and_empty_replies_still_fail(tmp_path):
    with_call = [
        ResponseStarted(response_id="call"),
        ToolCallCompleted(call_id="read", tool="test.read", arguments={}),
        ResponseCompleted(finish_reason="max_output_tokens"),
    ]
    empty = [
        ResponseStarted(response_id="empty"),
        ResponseCompleted(finish_reason="max_output_tokens"),
    ]
    for name, step in (("call", with_call), ("empty", empty)):
        tools = RecordingTools()
        turn = await run(tmp_path / name, ScriptedProvider([step, answer()]), tools=tools)
        assert turn.status is TurnStatus.FAILED
        assert turn.error.code == "provider_max_output_tokens"
        assert tools.calls == []
        assert not any(isinstance(item.content, ToolCallContent) for item in turn.items)


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
