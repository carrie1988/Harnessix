"""供应商中立契约；工厂将同一故障场景翻译为各供应商实际线协议。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.models import Budget, Item, ItemStatus, TextContent, Usage
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models.contracts import (
    ModelProvider,
    ModelRequest,
    ProviderEvent,
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


def assert_failed_attempts(
    events: Sequence[ProviderEvent], failure: ResponseFailed, count: int = 1
) -> None:
    """没有语义响应/用量的失败，也必须完整记录每次尝试，不能仅滤掉元数据。"""
    assert events[-1] == failure
    assert len(events) == count * 2 + 1
    identities = set()
    for index in range(count):
        start, end = events[index * 2 : index * 2 + 2]
        assert isinstance(start, ModelAttemptStarted)
        assert start.index == index + 1 and start.step == 1
        assert start.attempt_id not in identities
        identities.add(start.attempt_id)
        assert isinstance(end, ModelAttemptFinished)
        assert end.attempt_id == start.attempt_id and end.outcome == "failed"
        assert end.error.code == "provider_" + failure.code
        assert end.error.retryable == failure.retryable


class ProviderContract:
    async def test_text_and_usage(self, provider_factory: ProviderFactory) -> None:
        async with provider_factory("text") as probe:
            events = [
                event async for event in probe.provider.stream(model_request(), CancelToken())
            ]
            assert isinstance(events[0], ModelAttemptStarted)
            assert isinstance(events[1], ResponseStarted)
            assert isinstance(events[-1], ResponseCompleted)
            assert events[-1].usage == Usage(input_tokens=10, output_tokens=2)
            assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "你好"
            assert [e.text for e in events if isinstance(e, TextCompleted)] == ["你好"]
            observations = [e for e in events if isinstance(e, ModelUsageObserved)]
            assert observations[-1].usage.completeness == "complete"
            assert (
                observations[-1].usage.input_tokens == 10
                and observations[-1].usage.output_tokens == 2
            )
            assert observations[-1].actual_model == "test-model"
            assert observations[-1].response_id == events[1].response_id
            assert all(e.attempt_id == events[0].attempt_id for e in observations)
            ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
            assert len(ends) == 1 and ends[0].outcome == "completed"
            assert ends[0].attempt_id == events[0].attempt_id
            assert events.index(ends[0]) < len(events) - 1
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
            # 同一种错误可在 HTTP 阶段或流开始后出现；两种都不得释放工具/成功终态。
            assert events[-1] == ResponseFailed(code=code, retryable=retryable)
            assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)
            assert probe.attempts() == attempts
            starts = [e for e in events if isinstance(e, ModelAttemptStarted)]
            ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
            assert len(starts) == len(ends) == attempts
            assert [e.index for e in starts] == list(range(1, attempts + 1))
            assert len({e.attempt_id for e in starts}) == attempts
            assert [e.attempt_id for e in starts] == [e.attempt_id for e in ends]
            assert all(e.outcome == "failed" for e in ends)

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

    @pytest.mark.parametrize("after_response", [False, True])
    async def test_consumer_exit_closes_stream(
        self, provider_factory: ProviderFactory, after_response: bool
    ) -> None:
        async with provider_factory("text") as probe:
            async with aclosing(probe.provider.stream(model_request(), CancelToken())) as stream:
                assert isinstance(await anext(stream), ModelAttemptStarted)
                assert probe.attempts() == 0
                if after_response:
                    assert isinstance(await anext(stream), ResponseStarted)
                    assert probe.attempts() == 1
            if after_response:
                assert probe.closed()
            else:
                assert probe.attempts() == 0
