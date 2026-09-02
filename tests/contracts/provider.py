"""供应商中立契约；工厂将同一故障场景翻译为各供应商实际线协议。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.models import Budget, Item, ItemStatus, TextContent, Usage
from harnessix.models.contracts import (
    ModelProvider,
    ModelRequest,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    ToolCallCompleted,
)
from tests.agent.helpers import RecordingTools


def model_request(*, with_tools: bool = False) -> ModelRequest:
    return ModelRequest(
        thread_id=uuid4(),
        turn_id=uuid4(),
        step=1,
        history=(
            Item(
                item_id=uuid4(),
                status=ItemStatus.COMPLETED,
                content=TextContent(kind="user_message", text="测试任务"),
            ),
        ),
        tools=RecordingTools().definitions() if with_tools else (),
        budget=Budget(),
    )


@dataclass
class ProviderProbe:
    provider: ModelProvider
    entered: asyncio.Event
    closed: Callable[[], bool]
    attempts: Callable[[], int]


ProviderFactory = Callable[[str], AbstractAsyncContextManager[ProviderProbe]]


class ProviderContract:
    async def test_text_and_usage(self, provider_factory: ProviderFactory) -> None:
        async with provider_factory("text") as probe:
            events = [
                event async for event in probe.provider.stream(model_request(), CancelToken())
            ]
            assert isinstance(events[0], ResponseStarted)
            assert isinstance(events[-1], ResponseCompleted)
            assert events[-1].usage == Usage(input_tokens=10, output_tokens=2)
            assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "你好"
            assert [e.text for e in events if isinstance(e, TextCompleted)] == ["你好"]
            assert probe.closed()

    async def test_tool_fragments(self, provider_factory: ProviderFactory) -> None:
        async with provider_factory("tools") as probe:
            events = [
                event
                async for event in probe.provider.stream(
                    model_request(with_tools=True), CancelToken()
                )
            ]
            calls = [e for e in events if isinstance(e, ToolCallCompleted)]
            assert len(calls) == 2
            assert len({call.call_id for call in calls}) == 2
            assert [call.tool for call in calls] == ["test.read", "test.read"]
            assert [call.arguments for call in calls] == [{"path": "中文.txt"}, {}]
            assert isinstance(events[-1], ResponseCompleted)
            assert events[-1].finish_reason == "tool_calls"
            assert probe.closed()

    @pytest.mark.parametrize("scenario", ["bad_json", "truncated", "duplicate_finish"])
    async def test_invalid_stream_never_releases_tools(
        self, provider_factory: ProviderFactory, scenario: str
    ) -> None:
        async with provider_factory(scenario) as probe:
            events = [
                event
                async for event in probe.provider.stream(
                    model_request(with_tools=True), CancelToken()
                )
            ]
            assert events[-1] == ResponseFailed(code="invalid_provider_output")
            assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)
            assert probe.closed() and probe.attempts() == 1

    @pytest.mark.parametrize(
        ("scenario", "code", "retryable", "attempts"),
        [
            ("authentication", "authentication", False, 1),
            ("context_overflow", "context_overflow", False, 1),
            ("rate_limit", "rate_limit", True, 2),
        ],
    )
    async def test_error_codes_and_bounded_retry(
        self,
        provider_factory: ProviderFactory,
        scenario: str,
        code: str,
        retryable: bool,
        attempts: int,
    ) -> None:
        async with provider_factory(scenario) as probe:
            events = [
                event async for event in probe.provider.stream(model_request(), CancelToken())
            ]
            assert events == [ResponseFailed(code=code, retryable=retryable)]
            assert probe.attempts() == attempts

    async def test_midstream_failure_not_retried(self, provider_factory: ProviderFactory) -> None:
        async with provider_factory("midstream") as probe:
            events = [
                event async for event in probe.provider.stream(model_request(), CancelToken())
            ]
            assert any(isinstance(e, TextDelta) for e in events)
            assert isinstance(events[-1], ResponseFailed)
            assert not any(isinstance(e, ResponseCompleted) for e in events)
            assert probe.closed() and probe.attempts() == 1

    async def test_cancel_closes_blocked_stream(self, provider_factory: ProviderFactory) -> None:
        async with provider_factory("blocked") as probe:
            token = CancelToken()

            async def consume() -> None:
                async with aclosing(probe.provider.stream(model_request(), token)) as stream:
                    async for _ in stream:
                        pass

            task = asyncio.create_task(consume())
            await asyncio.wait_for(probe.entered.wait(), timeout=2)
            token.cancel()
            with pytest.raises(TurnCancelled):
                await asyncio.wait_for(task, timeout=2)
            assert probe.closed()

    async def test_consumer_exit_closes_stream(self, provider_factory: ProviderFactory) -> None:
        async with provider_factory("text") as probe:
            async with aclosing(probe.provider.stream(model_request(), CancelToken())) as stream:
                assert isinstance(await anext(stream), ResponseStarted)
            assert probe.closed()
