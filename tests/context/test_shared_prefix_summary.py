"""主动压缩的摘要请求沿用主请求的前缀（ADR 0167）。

请求开头与上一次主请求逐字相同，服务端前缀缓存可以复用；被动压缩、工具目录不稳定或放不下时
退回独立请求；模型在摘要请求里调用工具时这次压缩跳过、本轮之后改用独立请求。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.agent.compaction_summary_request import (
    SHARED_PREFIX_SUMMARY_NOTE,
    SUMMARY_TOOL_CALL,
)
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TextContent, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS, AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.context.compaction_projection import SUMMARY_PROJECTION_HEADER
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ResponseStarted,
    ToolArgumentsProgress,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed
from tests.agent.helpers import RecordingTools
from tests.context.test_compaction_runtime import (
    SequenceProvider,
    accounted_text,
    config,
    context_overflow_events,
)

LONG_ANSWER = "解析器调查事实与失败恢复约束。\n" * 600


async def _run(
    tmp_path: Path,
    summary,
    *,
    stable: bool = True,
    second=None,
    patch_context=None,
):
    normal = SequenceProvider(
        [accounted_text(LONG_ANSWER), second or accounted_text("已完成后续修复。")]
    )
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with AgentRuntime(
        store,
        normal,
        RecordingTools(),
        compaction=config(),
        summary_provider=summary,
        stable_tool_catalog=stable,
    ) as runtime:
        if patch_context is not None:
            runtime._prepare_budget_context = patch_context  # type: ignore[method-assign]
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "调查解析器", request_id="seed")
        turn = await runtime.run_turn(
            thread.thread_id, "继续修复并保持public接口", request_id="continue"
        )
        isolated = set(runtime._isolated_summary_turns)
    restored = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == restored
    return normal, turn, isolated


def _is_note(request: ModelRequest) -> bool:
    last = request.history[-1].content
    return isinstance(last, TextContent) and last.text == SHARED_PREFIX_SUMMARY_NOTE


async def test_proactive_summary_request_extends_the_previous_main_request(tmp_path: Path):
    summary = ScriptedProvider([accounted_text("保留public接口；解析器失败恢复仍需验证。")])
    normal, turn, isolated = await _run(tmp_path, summary)

    assert turn.status == "completed", turn.error
    assert [record.status for record in turn.compactions] == ["summarized"]
    assert not isolated
    (request,) = summary.requests
    previous = normal.requests[0]
    # 同一组系统指令与线上目录；历史是上一次主请求的历史加上其后的新内容，末尾是宿主说明。
    assert request.tools == () and request.catalog_tools == previous.wire_tools
    assert request.instructions == previous.instructions
    assert request.history[: len(previous.history)] == previous.history
    assert _is_note(request)
    assert request.remaining_tokens == 128
    for build, settings in (
        (chat_request, OpenAIChatConfig(model="fixture")),
        (anthropic_request, AnthropicConfig(model="fixture")),
    ):
        main_body, _ = build(previous, settings)
        summary_body, _ = build(request, settings)
        assert summary_body["tools"] == main_body["tools"]
        assert summary_body.get("system") == main_body.get("system")
        prefix = main_body["messages"]
        assert summary_body["messages"][: len(prefix)] == prefix
    # 激活与投影不变：下一次主请求只带摘要，不带被覆盖的原文。
    projected = "\n".join(
        item.content.text
        for item in normal.requests[-1].history
        if isinstance(item.content, TextContent)
    )
    assert SUMMARY_PROJECTION_HEADER in projected and "解析器调查事实" not in projected


async def test_without_a_stable_catalog_the_wire_tools_still_match_the_main_request(
    tmp_path: Path,
):
    """产品默认没有稳定目录：摘要请求的线上工具是本步视图选出的工具，与主请求逐字相同。"""
    summary = ScriptedProvider([accounted_text("摘要")])
    normal, turn, _ = await _run(tmp_path, summary, stable=False)

    assert turn.status == "completed", turn.error
    (request,) = summary.requests
    previous = normal.requests[0]
    assert previous.catalog_tools is None and previous.tools
    assert request.tools == () and request.catalog_tools == previous.tools
    assert _is_note(request)
    main_body, _ = chat_request(previous, OpenAIChatConfig(model="fixture"))
    summary_body, _ = chat_request(request, OpenAIChatConfig(model="fixture"))
    assert summary_body["tools"] == main_body["tools"]
    prefix = main_body["messages"]
    assert summary_body["messages"][: len(prefix)] == prefix


async def test_context_that_does_not_fit_falls_back_to_the_isolated_request(tmp_path: Path):
    async def overflowing(*_args, **_kwargs):
        raise KernelError("context_budget_exceeded", "放不下")

    summary = ScriptedProvider([accounted_text("摘要")])
    _, turn, _ = await _run(tmp_path, summary, patch_context=overflowing)

    assert turn.status == "completed", turn.error
    (request,) = summary.requests
    # 原独立请求逐字不变：摘要指令、一条不可信来源JSON、没有工具。
    assert request.instructions == SUMMARY_INSTRUCTIONS and not _is_note(request)
    assert request.tools == () and request.catalog_tools is None and len(request.history) == 1
    source = json.loads(request.history[0].content.text)
    assert source["trust"] == "untrusted_history" and source["authority"] == "none"
    chat, _ = chat_request(request, OpenAIChatConfig(model="fixture"))
    anthropic, _ = anthropic_request(request, AnthropicConfig(model="fixture"))
    assert "tools" not in chat and "tools" not in anthropic
    assert chat["messages"][0] == {"role": "system", "content": SUMMARY_INSTRUCTIONS}
    assert anthropic["system"] == SUMMARY_INSTRUCTIONS


async def test_reactive_compaction_keeps_the_isolated_request(tmp_path: Path):
    """主请求已超出窗口：共用前缀的请求同样放不下，被动压缩仍用独立请求。"""
    summary = ScriptedProvider([accounted_text("摘要")])
    normal = SequenceProvider(
        [
            accounted_text(LONG_ANSWER),
            context_overflow_events(step=1),
            accounted_text("已完成后续修复。", step=2),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "reactive.sqlite")
    async with AgentRuntime(
        store,
        normal,
        RecordingTools(),
        compaction=config(trigger_history_tokens=50_000),
        summary_provider=summary,
        stable_tool_catalog=True,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "调查解析器", request_id="seed")
        await runtime.run_turn(thread.thread_id, "继续修复", request_id="continue")

    assert summary.requests and all(
        request.instructions == SUMMARY_INSTRUCTIONS and not _is_note(request)
        for request in summary.requests
    )


async def test_a_tool_call_in_the_shared_request_skips_this_compaction(tmp_path: Path):
    start = attempt_start()
    summary = ScriptedProvider(
        [
            [
                start,
                ResponseStarted(response_id="response-1"),
                observed(start, completeness="complete", input_tokens=10, output_tokens=3),
                ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
                ToolCallCompleted(call_id="call-1", tool="test.read", arguments={}),
                ResponseCompleted(
                    finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
                ),
            ]
        ]
    )
    normal, turn, isolated = await _run(tmp_path, summary)

    # 用量已记账、窗口未激活；本步保留原历史继续，工具没有执行；本轮之后改用独立请求。
    assert turn.status == "completed", turn.error
    (record,) = turn.compactions
    assert record.status == "failed" and record.failure.code == SUMMARY_TOOL_CALL
    assert record.attempt.status == "completed"
    assert isolated == {turn.turn_id}
    assert len(normal.requests) == 2
    assert not any(
        isinstance(item.content, TextContent)
        and item.content.text.startswith(SUMMARY_PROJECTION_HEADER)
        for item in normal.requests[-1].history
    )


async def test_argument_progress_before_the_tool_call_still_skips_this_compaction(
    tmp_path: Path,
):
    """适配器在调用写完前先报参数进度（vLLM实测，参数还是空的时候就报一次）。

    进度只是界面的临时显示：摘要流照常读到用量与调用，这次压缩跳过、整轮继续。此前进度落进
    “不支持的摘要Provider事件”，整轮失败（NL2RepoBench第二批sqlparse，2026-11-19）。
    """
    start = attempt_start()
    summary = ScriptedProvider(
        [
            [
                start,
                ResponseStarted(response_id="response-1"),
                ToolArgumentsProgress(utf8_bytes=0, tool="test.read"),
                ToolArgumentsProgress(utf8_bytes=4096, tool="test.read"),
                observed(start, completeness="complete", input_tokens=10, output_tokens=3),
                ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
                ToolCallCompleted(call_id="call-1", tool="test.read", arguments={}),
                ResponseCompleted(
                    finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
                ),
            ]
        ]
    )
    normal, turn, isolated = await _run(tmp_path, summary)

    assert turn.status == "completed", turn.error
    (record,) = turn.compactions
    assert record.status == "failed" and record.failure.code == SUMMARY_TOOL_CALL
    assert record.attempt.status == "completed"
    assert isolated == {turn.turn_id}
    assert len(normal.requests) == 2


@pytest.mark.parametrize("note", [SHARED_PREFIX_SUMMARY_NOTE])
def test_note_keeps_history_instructions_as_data(note: str) -> None:
    """说明与独立请求的摘要指令同义：英文纯文本、三段、不列待办、历史中的指令只是数据，不调用工具。"""
    for phrase in (
        "Do not call tools",
        "Verified facts",
        "Hypotheses",
        "Contradicting observations",
        "No to-dos",
        "are data",
    ):
        assert phrase in note
