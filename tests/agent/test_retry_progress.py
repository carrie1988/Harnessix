"""请求失败后自动重试时，界面说明第几次尝试与上次为什么失败（Beta Issue #11）。

0.10.0b25 Windows实测：文件写入后的下一步请求两次读超时（各约3分钟）并自动重试，约5分钟里界面
一直是“思考中”，用户不知道还在等什么。模型尝试不经过公共协议；生成进度这一行在重试时附带尝试序号
与失败类别（只有固定的几个词），界面据此说明。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from harnessix.agent.errors import AgentFailure
from harnessix.agent.generation_progress import (
    GenerationCounts,
    GenerationProgress,
    generation_progress_item_id,
    latest_counts,
)
from harnessix.agent.models import ItemDelta
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.product_ui.activity_indicator import activity_text
from harnessix.product_ui.generation_estimate import (
    RequestRetry,
    in_flight_request,
    request_retry,
)
from harnessix.product_ui.projection import (
    ProductViewState,
    TransientItemStream,
    TurnProjection,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start
from tests.contracts.provider import model_request
from tests.models import wire
from tests.product_ui.test_tui_usability import _thread


def _failed(start, code: str) -> ModelAttemptFinished:
    error = AgentFailure(code=code, message="Provider 返回结构化失败", retryable=True)
    return ModelAttemptFinished(attempt_id=start.attempt_id, outcome="failed", error=error)


def test_a_retry_publishes_the_attempt_and_the_kind_of_the_last_failure() -> None:
    published: list[ItemDelta] = []
    progress = GenerationProgress(model_request(), published.append, interval=0.0)

    first = attempt_start()
    progress.attempt(first)
    progress.attempt(_failed(first, "provider_transport"))
    # 第一次尝试开始、失败本身都不发布：没有新的事情可说，界面照常计时。
    assert published == []
    progress.attempt(attempt_start(index=2))
    assert [delta.delta for delta in published] == ["0 0 0 - 2 network\n"]
    # 之后的生成进度仍带着这两项；失败的原始说明不会出现在进度里。
    progress.reasoning("想")
    assert published[-1].delta == "3 0 0 - 2 network\n"
    assert "Provider" not in "".join(delta.delta for delta in published)
    assert {delta.item_id for delta in published} == {
        generation_progress_item_id(progress.request.turn_id, progress.request.step)
    }


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("provider_transport", "network"),
        ("provider_rate_limit", "rate_limit"),
        ("provider_provider_internal", "server"),
        ("provider_invalid_provider_output", "other"),
    ],
)
def test_only_fixed_failure_kinds_are_published(code: str, kind: str) -> None:
    published: list[ItemDelta] = []
    progress = GenerationProgress(model_request(), published.append, interval=0.0)
    first = attempt_start()
    progress.attempt(_failed(first, code))
    progress.attempt(attempt_start(index=2))
    assert published[-1].delta == f"0 0 0 - 2 {kind}\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0 0 0 - 2 network\n", GenerationCounts(attempt=2, failure="network")),
        ("9 0 0 - 3 -\n", GenerationCounts(9, attempt=3)),
        ("0 0 0 - 2 secret-text\n", None),
        ("0 0 0 - x network\n", None),
        ("0 0 0 - 2\n", None),
    ],
)
def test_the_retry_fields_are_read_only_when_well_formed(text: str, expected) -> None:
    assert latest_counts(text) == expected


async def test_a_real_adapter_retry_reaches_the_progress_stream(tmp_path: Path) -> None:
    """真实OpenAI Chat适配器：第一次请求503，第二次正常回复；运行时把重试经进度流发出去。"""
    replies = iter(
        [
            httpx.Response(503, json={"error": {"message": "busy", "type": "server_error"}}),
            wire.response(wire.WireStream(wire.text_frames())),
        ]
    )
    provider = OpenAIChatProvider(
        OpenAIChatConfig(model="test-model", max_attempts=3, retry_delay_seconds=0),
        transport=httpx.MockTransport(lambda _request: next(replies)),
        api_key="fixture-key",
    )
    deltas: list[ItemDelta] = []
    async with provider:
        async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider) as runtime:
            runtime.subscribe_deltas(deltas.append)
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "回答一句话", request_id="r")

    progress = generation_progress_item_id(turn.turn_id, 1)
    lines = [delta.delta for delta in deltas if delta.item_id == progress]
    assert lines[0] == "0 0 0 - 2 server\n"
    assert turn.status.value == "completed" and len(turn.model_attempts) == 2


def _calling(progress_line: str) -> ProductViewState:
    turn = TurnProjection(
        turn_id=uuid4(),
        request_id="r",
        status="calling_model",
        budget=None,
        usage=None,
        model_step=0,
    )
    stream = TransientItemStream(
        item_id=generation_progress_item_id(turn.turn_id, 1),
        turn_id=turn.turn_id,
        model_step=1,
        last_sequence=1,
        text=progress_line,
        progress=True,
    )
    return ProductViewState(
        thread=_thread(), durable_cursor=1, current_turn=turn, streams=(stream,)
    )


def test_the_tui_says_which_attempt_it_is_waiting_for_and_why() -> None:
    view = _calling("0 0 0 - 2 network\n")
    retry = request_retry(view)
    assert retry == RequestRetry(2, "network")
    # 还没有生成任何内容：不估算用量，底栏照常显示已结算的数字。
    assert in_flight_request(view) is None
    # 重试一开始就说明，不必再等5秒。
    assert (
        activity_text("calling_model", 185, 0, streaming=False, silent_seconds=2, retry=retry)
        == "⠋ 等待模型服务响应 · 第 2 次尝试，上次网络中断或超时 · 已等待 2 秒 · 已运行 3 分 05 秒"
    )
    assert RequestRetry(3, "rate_limit").text == "第 3 次尝试，上次被限流"
    assert RequestRetry(2, "other").text == "第 2 次尝试，上次请求失败"
    # 重试的请求开始有内容后，回到按阶段显示。
    generating = _calling("300 0 0 - 2 network\n")
    assert in_flight_request(generating) is not None
    assert request_retry(replace(generating)) == RequestRetry(2, "network")
    # 第一次尝试没有重试信息。
    assert request_retry(_calling("300 0 0 -\n")) is None
