from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.models import Budget, Item, ItemStatus, TextContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
)
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.soak_provider import SoakProvider, SoakSummaryProvider


def _request(prompt: str) -> ModelRequest:
    return ModelRequest(
        thread_id=uuid4(),
        turn_id=uuid4(),
        step=1,
        history=(
            Item(
                item_id=uuid4(),
                status=ItemStatus.COMPLETED,
                content=TextContent(kind="user_message", text=prompt),
            ),
        ),
        tools=(),
        budget=Budget(),
    )


async def _events(provider: SoakProvider) -> list[object]:
    return [event async for event in provider.stream(_request("输入正文"), CancelToken())]


async def test_stream_emits_fixed_sequence_and_counts_requests() -> None:
    provider = SoakProvider()

    events = await _events(provider)

    assert [type(event) for event in events] == [
        ResponseStarted,
        TextStarted,
        TextDelta,
        TextCompleted,
        ResponseCompleted,
    ]
    assert events[2].delta == SoakProvider.RESPONSE_TEXT
    assert events[3].text == SoakProvider.RESPONSE_TEXT
    assert provider.request_count == 1
    assert not hasattr(provider, "requests")
    assert not hasattr(provider, "__dict__")


async def test_cancel_before_first_event_does_not_retain_request() -> None:
    provider = SoakProvider()
    cancel = CancelToken()
    cancel.cancel()
    stream = provider.stream(_request("不得保留的请求正文"), cancel)

    with pytest.raises(TurnCancelled):
        await anext(stream)
    await stream.aclose()

    assert provider.request_count == 1
    assert not hasattr(provider, "requests")
    assert not hasattr(provider, "__dict__")


async def test_runtime_completes_ten_continuous_turns_without_request_history(tmp_path) -> None:
    provider = SoakProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")

    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turns = [
            await runtime.run_turn(
                thread.thread_id,
                f"长会话输入正文-{index}-不应进入Provider状态",
                request_id=f"soak-turn-{index}",
            )
            for index in range(10)
        ]

    assert all(turn.status is TurnStatus.COMPLETED for turn in turns)
    assert provider.request_count == 10
    assert not hasattr(provider, "requests")
    assert not hasattr(provider, "__dict__")
    assert SoakProvider.__slots__ == ("request_count",)
    assert isinstance(provider.request_count, int)


async def test_summary_provider_emits_complete_attempt_without_request_retention() -> None:
    provider = SoakSummaryProvider()
    events = [event async for event in provider.stream(_request("敏感历史"), CancelToken())]

    assert isinstance(events[0], ModelAttemptStarted)
    assert isinstance(events[4], ModelUsageObserved)
    assert isinstance(events[5], ModelAttemptFinished)
    assert isinstance(events[-1], ResponseCompleted)
    assert events[-1].usage is not None and events[-1].usage.total_tokens == 13
    assert provider.request_count == 1
    assert not hasattr(provider, "requests") and not hasattr(provider, "__dict__")
