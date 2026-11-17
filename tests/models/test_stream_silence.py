"""流式响应的静默期限（ADR 0166）。

工具调用开始之前，连续io_timeout_seconds收不到字节仍按读取超时重试；工具调用开始之后，服务端
可能把数组、对象参数攒到写完才整段发出（实测vLLM），静默只受本次请求的总期限约束。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ResponseFailed, ToolCallCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.contracts.provider import model_request
from tests.models import anthropic_wire, wire

IDLE = 0.1
LONG_PAUSE = 0.4
KEY = "fixture-SILENCE-KEY"

Paced = list[tuple[float, bytes]]


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESSIX_TEST_KEY", KEY)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)


class _Paced:
    """按(停顿秒数, 字节)逐段发送；记录请求次数与关闭。"""

    def __init__(self, parts: Paced) -> None:
        self.parts = parts
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for pause, part in self.parts:
            if pause:
                await asyncio.sleep(pause)
            yield part

    async def aclose(self) -> None:
        self.closed += 1


class _OpenAIPaced(_Paced, httpx.AsyncByteStream):
    pass


class _AnthropicPaced(_Paced, httpx2.AsyncByteStream):
    pass


def _openai_parts(*, before_tool: float = 0, after_tool: float = 0, beats: int = 0) -> Paced:
    header = {
        "index": 0,
        "id": "wire-call-0",
        "type": "function",
        "function": {"name": tool_alias("test.read"), "arguments": ""},
    }
    keepalive = [(LONG_PAUSE / beats, b": keepalive\n\n") for _ in range(beats)]
    return [
        (0, wire.frame(wire.chunk({"role": "assistant", "content": "写文件"}))),
        *keepalive,
        (before_tool, wire.frame(wire.chunk({"tool_calls": [header]}))),
        (
            after_tool,
            wire.frame(wire.chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})),
        ),
        (0, wire.frame(wire.chunk(finish="tool_calls"))),
        (0, wire.frame(wire.chunk(usage=True))),
        (0, b"data: [DONE]\n\n"),
    ]


def _anthropic_parts(*, before_tool: float = 0, after_tool: float = 0, beats: int = 0) -> Paced:
    start, delta, stop = anthropic_wire.tool_block()
    keepalive = [(LONG_PAUSE / beats, anthropic_wire.frame("ping")) for _ in range(beats)]
    return [
        (0, anthropic_wire.start()),
        *keepalive,
        (before_tool, start),
        (after_tool, delta),
        (0, stop),
        *[(0, part) for part in anthropic_wire.stop("tool_use")],
    ]


async def _run(kind: str, parts: Paced, **limits: Any) -> tuple[list[Any], int]:
    requests = 0
    if kind == "openai":

        def handle_openai(_: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return wire.response(_OpenAIPaced(parts))  # type: ignore[arg-type]

        settings = OpenAIChatConfig(
            model="test-model", api_key_env="HARNESSIX_TEST_KEY", retry_delay_seconds=0, **limits
        )
        provider: Any = OpenAIChatProvider(settings, transport=httpx.MockTransport(handle_openai))
    else:

        def handle_anthropic(_: httpx2.Request) -> httpx2.Response:
            nonlocal requests
            requests += 1
            return anthropic_wire.response(_AnthropicPaced(parts))  # type: ignore[arg-type]

        settings_a = AnthropicConfig(
            model="test-model", api_key_env="HARNESSIX_TEST_KEY", retry_delay_seconds=0, **limits
        )
        provider = AnthropicProvider(settings_a, transport=httpx2.MockTransport(handle_anthropic))
    async with provider:
        request = model_request(with_tools=True)
        events = [e async for e in provider.stream(request, CancelToken())]
    return events, requests


def _parts(kind: str, **pauses: Any) -> Paced:
    return _openai_parts(**pauses) if kind == "openai" else _anthropic_parts(**pauses)


ADAPTERS = pytest.mark.parametrize("kind", ["openai", "anthropic"])


@ADAPTERS
async def test_silence_after_a_tool_call_starts_waits_for_the_arguments(kind: str) -> None:
    """参数整段晚到（静默长于读取期限）：同一次尝试照常完成，不作废重来。"""
    events, requests = await _run(
        kind, _parts(kind, after_tool=LONG_PAUSE), io_timeout_seconds=IDLE, max_attempts=2
    )
    assert requests == 1
    assert [e for e in events if isinstance(e, ToolCallCompleted)]
    finished = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert [e.outcome for e in finished] == ["completed"]


@ADAPTERS
async def test_silence_before_any_tool_call_is_still_a_read_timeout(kind: str) -> None:
    """工具调用开始之前的静默沿用原语义：按读取超时作废并重试。"""
    events, requests = await _run(
        kind, _parts(kind, before_tool=LONG_PAUSE), io_timeout_seconds=IDLE, max_attempts=2
    )
    assert requests == 2
    assert events[-1] == ResponseFailed(code="transport", retryable=True)
    finished = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert [e.outcome for e in finished] == ["failed", "failed"]
    if kind == "openai":
        assert all(
            e.error is not None and e.error.message.endswith("chat_transport/v1:httpx_read_timeout")
            for e in finished
        )


@ADAPTERS
async def test_any_bytes_count_as_activity_before_a_tool_call(kind: str) -> None:
    """心跳与SSE注释也是到达的字节：间隔短于读取期限就不算静默（与HTTP读取期限相同）。"""
    events, requests = await _run(kind, _parts(kind, beats=8), io_timeout_seconds=IDLE)
    assert requests == 1
    assert [e for e in events if isinstance(e, ToolCallCompleted)]


@ADAPTERS
async def test_the_request_deadline_still_bounds_a_started_tool_call(kind: str) -> None:
    """工具调用开始之后仍受本次请求的总期限约束，到期不再重试。"""
    loop = asyncio.get_running_loop()
    began = loop.time()
    events, requests = await _run(
        kind,
        _parts(kind, after_tool=5.0),
        io_timeout_seconds=IDLE,
        timeout_seconds=0.3,
        max_attempts=3,
    )
    assert loop.time() - began < 2.0
    assert requests == 1
    assert events[-1] == ResponseFailed(code="transport", retryable=True)
    assert sum(isinstance(e, ModelAttemptStarted) for e in events) == 1
