"""回复中途断开：断开前只收到过临时内容（思考、工具参数进度）时自动重试。

负责人2026-11-13决定“先做A”。

0.10.0b30 macOS正式测试的会话记录：17次失败的尝试里16次断开前没有任何正文，
其中多次已经思考了46～148秒，断开后整轮直接结束，要用户发送“继续”。思考预览与生成进度只作临时增量，不建Item、不进模型历史，
丢弃后重来是安全的；已经出过正文后断开仍按原来结束（B，另行决定）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import httpx2
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure
from harnessix.agent.generation_progress import generation_progress_item_id
from harnessix.agent.models import Budget, ItemDelta, TextContent
from harnessix.agent.reasoning_preview import ReasoningPreview, reasoning_item_id
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.turn_budget_compaction import UnknownUsagePolicy
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ReasoningDelta,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    ToolArgumentsProgress,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.secrets.text_publication import SecretTextStep
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import accounted_answer, attempt_start
from tests.contracts.provider import model_request
from tests.models import anthropic_wire
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames
from tests.product_config.conftest import config as config  # noqa: F401  （夹具）
from tests.product_config.test_service_failure_retries import _run

# 付费端点的交互模式：服务方故障且用量未知时最多连续重试5次（ADR 0144），与产品装配相同。
PAID_INTERACTIVE = UnknownUsagePolicy(service_failure_retries=5)
# 交互模式的单轮预算（费用未知的云模型300万Token）：用量未知的尝试按保守额度预留，默认的10万放不下。
INTERACTIVE_BUDGET = Budget(max_tokens=3_000_000)


def _thinking_then_break() -> WireStream:
    """开始流式、推送几段思考后连接断开（与正式测试里的httpx协议错误同类）。"""
    parts = [
        frame(chunk({"role": "assistant", "reasoning_content": "先读一下"})),
        frame(chunk({"reasoning_content": "目录结构"})),
    ]
    return WireStream(parts, fail=True)


def _thinking_and_arguments_then_break() -> WireStream:
    """思考之后开始生成工具参数（预览已收尾），参数没写完就断开。"""
    parts = [
        frame(chunk({"role": "assistant", "reasoning_content": "先读一下"})),
        frame(chunk({"tool_calls": [call(arguments='{"path": "a')]})),
    ]
    return WireStream(parts, fail=True)


def _thinking_then_answer() -> WireStream:
    return WireStream(
        [frame(chunk({"role": "assistant", "reasoning_content": "换个思路"})), *text_frames()]
    )


def _reply_then_break() -> WireStream:
    return WireStream([frame(chunk({"role": "assistant", "content": "已经开始写"}))], fail=True)


def _provider(
    streams: list[WireStream], requests: list[httpx.Request], **limits
) -> OpenAIChatProvider:  # type: ignore[no-untyped-def]
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(streams[len(requests) - 1])

    config = OpenAIChatConfig(
        model="test-model", retry_delay_seconds=0, **{"max_attempts": 3, **limits}
    )
    return OpenAIChatProvider(config, transport=httpx.MockTransport(handle), api_key="fixture-key")


async def test_a_break_during_thinking_is_retried_in_the_adapter() -> None:
    requests: list[httpx.Request] = []
    streams = [_thinking_then_break(), WireStream(text_frames())]
    async with _provider(streams, requests) as provider:
        events = [e async for e in provider.stream(model_request(with_tools=True), CancelToken())]
    assert len(requests) == 2
    starts = [e for e in events if isinstance(e, ModelAttemptStarted)]
    ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert [e.index for e in starts] == [1, 2]
    assert [e.outcome for e in ends] == ["failed", "completed"]
    assert ends[0].error is not None and ends[0].error.code == "provider_transport"
    # 断开的那次请求可能已经计费：用量按“未知”记，不是零。
    first_usage = [
        e
        for e in events
        if isinstance(e, ModelUsageObserved) and e.attempt_id == starts[0].attempt_id
    ]
    assert all(e.usage.completeness != "complete" for e in first_usage)
    assert isinstance(events[-1], ResponseCompleted)
    assert not any(isinstance(e, ResponseFailed) for e in events)


async def test_a_break_after_reply_text_started_still_ends_the_request() -> None:
    requests: list[httpx.Request] = []
    streams = [_reply_then_break(), WireStream(text_frames())]
    async with _provider(streams, requests) as provider:
        events = [e async for e in provider.stream(model_request(with_tools=True), CancelToken())]
    assert len(requests) == 1
    assert events[-1] == ResponseFailed(code="transport", retryable=True)


def _anthropic_tool_arguments_then_break() -> anthropic_wire.WireStream:
    """Anthropic：响应开始、工具参数写到一半断开（这个适配器暂不转发思考，参数进度同为临时内容）。"""
    block = anthropic_wire.tool_block(arguments='{"path": "a')[:2]
    return anthropic_wire.WireStream([anthropic_wire.start(), *block], fail=True)


def _anthropic_reply_then_break() -> anthropic_wire.WireStream:
    parts = anthropic_wire.text_frames()[:3]
    return anthropic_wire.WireStream(parts, fail=True)


async def _anthropic_events(
    streams: list[anthropic_wire.WireStream],
) -> tuple[int, list[object]]:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return anthropic_wire.response(streams[len(requests) - 1])

    config = AnthropicConfig(model="test-model", retry_delay_seconds=0, max_attempts=3)
    async with AnthropicProvider(
        config, transport=httpx2.MockTransport(handle), api_key="fixture-key"
    ) as provider:
        events = [e async for e in provider.stream(model_request(with_tools=True), CancelToken())]
    return len(requests), events


async def test_anthropic_retries_a_break_before_any_reply_the_same_way() -> None:
    count, events = await _anthropic_events(
        [
            _anthropic_tool_arguments_then_break(),
            anthropic_wire.WireStream(anthropic_wire.text_frames()),
        ]
    )
    assert count == 2
    assert any(isinstance(e, ToolArgumentsProgress) for e in events)
    ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert [e.outcome for e in ends] == ["failed", "completed"]
    assert isinstance(events[-1], ResponseCompleted)


async def test_anthropic_still_ends_the_request_after_reply_text_started() -> None:
    count, events = await _anthropic_events(
        [_anthropic_reply_then_break(), anthropic_wire.WireStream(anthropic_wire.text_frames())]
    )
    assert count == 1
    assert events[-1] == ResponseFailed(code="transport", retryable=True)


async def test_the_runtime_accepts_the_retry_and_the_turn_completes(tmp_path: Path) -> None:
    """运行时：没有提交过任何正文Item时接受新的尝试；思考预览与生成进度随新尝试继续。"""
    requests: list[httpx.Request] = []
    streams = [_thinking_then_break(), WireStream(text_frames())]
    deltas: list[ItemDelta] = []
    async with _provider(streams, requests) as provider:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, unknown_usage=PAID_INTERACTIVE
        ) as runtime:
            runtime.subscribe_deltas(deltas.append)
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id, "回答一句话", request_id="r", budget=INTERACTIVE_BUDGET
            )
    assert turn.status.value == "completed", turn.error
    assert [a.status for a in turn.model_attempts] == ["failed", "completed"]
    assert turn.model_attempts[0].usage.completeness != "complete"
    texts = [
        i.content.text
        for i in turn.items
        if isinstance(i.content, TextContent) and i.content.kind == "assistant_message"
    ]
    assert texts == ["你好"]
    # 断开前的思考已经实时显示过；重试开始时进度带上第2次尝试与上次失败的类别。
    assert any(d.item_id == reasoning_item_id(turn.turn_id, 1) for d in deltas)
    progress = [
        d.delta for d in deltas if d.item_id == generation_progress_item_id(turn.turn_id, 1)
    ]
    assert any(line.endswith(" 2 network\n") for line in progress), progress


async def test_the_new_attempt_thinks_in_a_new_paragraph_of_the_preview(tmp_path: Path) -> None:
    """预览在生成工具参数时已收尾；新尝试的思考空一行接着显示，不丢、不和上一段连在一起。"""
    requests: list[httpx.Request] = []
    streams = [_thinking_and_arguments_then_break(), _thinking_then_answer()]
    deltas: list[ItemDelta] = []
    async with _provider(streams, requests) as provider:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, unknown_usage=PAID_INTERACTIVE
        ) as runtime:
            runtime.subscribe_deltas(deltas.append)
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id, "回答一句话", request_id="r", budget=INTERACTIVE_BUDGET
            )
    assert turn.status.value == "completed", turn.error
    preview = [d for d in deltas if d.item_id == reasoning_item_id(turn.turn_id, 1)]
    assert "".join(d.delta for d in preview) == "先读一下\n\n换个思路"
    assert [d.stream_sequence for d in preview] == list(range(1, len(preview) + 1))


async def test_a_finished_preview_segment_is_never_written_again() -> None:
    """真实的公开保护步骤拒绝向已收尾的文本块写入；新尝试的思考必须换一个保护身份。"""
    request = model_request(with_tools=True)
    step = SecretTextStep((b"not-a-real-secret",), lambda: None)
    shown: list[ItemDelta] = []
    preview = ReasoningPreview(request, CancelToken(), step, shown.append)
    assert await preview.observe(ReasoningDelta(delta="先读一下"))
    assert not await preview.observe(ToolArgumentsProgress(utf8_bytes=8, tool="test.read"))
    await preview.restart()
    assert await preview.observe(ReasoningDelta(delta="换个思路"))
    assert not await preview.observe(ResponseCompleted())
    assert "".join(d.delta for d in shown) == "先读一下\n\n换个思路"


async def test_the_runtime_still_ends_the_turn_when_reply_text_had_started(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    streams = [_reply_then_break(), WireStream(text_frames())]
    async with _provider(streams, requests) as provider:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, unknown_usage=PAID_INTERACTIVE
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id, "回答一句话", request_id="r", budget=INTERACTIVE_BUDGET
            )
    assert len(requests) == 1
    assert turn.status.value == "failed" and turn.error is not None
    assert turn.error.code == "provider_transport"


def _thinking_then_break_then_answer():  # type: ignore[no-untyped-def]
    """产品装配用的脚本：第1次尝试开始流式、思考后断开（用量未知），第2次正常回答。"""
    first, second = attempt_start(), attempt_start(index=2)
    broken = ModelAttemptFinished(
        attempt_id=first.attempt_id,
        outcome="failed",
        error=AgentFailure(code="provider_transport", message="连接中断", retryable=True),
    )
    return [
        first,
        ResponseStarted(response_id="response-1"),
        ReasoningDelta(delta="先读一下目录结构"),
        broken,
        *accounted_answer(start=second),
    ]


@pytest.mark.parametrize(
    ("retries", "expected"),
    [(None, ("completed", None)), (0, ("failed", "provider_transport"))],
)
async def test_the_product_retries_within_the_configured_limit(
    tmp_path: Path, config, monkeypatch, retries, expected
) -> None:  # type: ignore[no-untyped-def]
    """真实的产品装配（含压缩与付费交互模式的用量策略）：默认重试；设为0时断开的请求可能已计费，不再请求。"""
    chosen = config.model_copy(update={"service_failure_retries": retries})
    status, error, _ = await _run(tmp_path, chosen, monkeypatch, _thinking_then_break_then_answer)
    assert (status, error) == expected
