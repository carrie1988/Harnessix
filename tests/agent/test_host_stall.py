"""宿主卡顿重做的边界：只重做认定的超时，最多三次，等待期间可以取消（ADR 0177）。"""

from __future__ import annotations

import asyncio

import pytest

from harnessix.agent import host_stall
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.host_stall import retry_after_host_stall


@pytest.fixture(autouse=True)
def _short_delays(monkeypatch):
    monkeypatch.setattr(host_stall, "STALL_RETRY_DELAYS_SECONDS", (0.0, 0.0))


def _attempts(*outcomes):
    calls = []

    async def attempt():
        outcome = outcomes[len(calls)]
        calls.append(outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return attempt, calls


def _timeout(error: Exception) -> bool:
    return isinstance(error, TimeoutError)


async def test_a_stall_followed_by_success_returns_the_success():
    attempt, calls = _attempts(TimeoutError(), "done")
    assert await retry_after_host_stall(attempt, stalled=_timeout, token=CancelToken()) == "done"
    assert len(calls) == 2


async def test_three_stalls_raise_the_last_timeout():
    attempt, calls = _attempts(TimeoutError(), TimeoutError(), TimeoutError("third"))
    with pytest.raises(TimeoutError, match="third"):
        await retry_after_host_stall(attempt, stalled=_timeout, token=CancelToken())
    assert len(calls) == 3


async def test_other_errors_are_not_retried():
    attempt, calls = _attempts(ValueError("broken"), "never")
    with pytest.raises(ValueError):
        await retry_after_host_stall(attempt, stalled=_timeout, token=CancelToken())
    assert len(calls) == 1


async def test_cancel_during_the_wait_stops_without_another_attempt(monkeypatch):
    monkeypatch.setattr(host_stall, "STALL_RETRY_DELAYS_SECONDS", (30.0, 30.0))
    attempt, calls = _attempts(TimeoutError(), "never")
    waiting = asyncio.Event()

    def stalled(error: Exception) -> bool:
        waiting.set()
        return _timeout(error)

    token = CancelToken()
    task = asyncio.create_task(retry_after_host_stall(attempt, stalled=stalled, token=token))
    await asyncio.wait_for(waiting.wait(), 5)
    token.cancel()
    with pytest.raises(TurnCancelled):
        await asyncio.wait_for(task, 5)
    assert len(calls) == 1
