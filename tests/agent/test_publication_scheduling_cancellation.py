"""调度超预算与取消的交叉边界；复用纯端口夹具，不改变正式扫描期限。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from harnessix.agent import publication as boundary
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from tests.agent.test_publication_scheduling import Guard, scan


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
@pytest.mark.parametrize("cancellation", ["token", "parent"])
async def test_delayed_tail_still_delivers_queued_cancellation(monkeypatch, kind, cancellation):
    clock = [boundary.monotonic()]
    original_sleep = asyncio.sleep
    handoffs = []
    token = CancelToken()

    async def queued(delay):
        if sys._getframe(1).f_code.co_name == "_protect":
            handoffs.append(delay)
            if len(handoffs) == 2:
                clock[0] += boundary.PUBLIC_PROTECTION_TIMEOUT + 1
        await original_sleep(delay)

    async def operation():
        task = asyncio.current_task()
        assert task is not None
        cancel = token.cancel if cancellation == "token" else task.cancel
        await scan(kind, Guard(lambda: asyncio.get_running_loop().call_soon(cancel)), token)

    expected = TurnCancelled if cancellation == "token" else asyncio.CancelledError
    with monkeypatch.context() as patch:
        patch.setattr(boundary, "monotonic", lambda: clock[0])
        patch.setattr(asyncio, "sleep", queued)
        with pytest.raises(expected):
            await asyncio.create_task(operation())
    assert handoffs == [0, 0]


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
async def test_outer_deadline_at_tail_is_not_public_guard_failure(kind):
    guard = None
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(None) as outer:
            guard = Guard(lambda: outer.reschedule(asyncio.get_running_loop().time()))
            await scan(kind, guard, CancelToken())
    assert guard is not None and guard.calls == 1


@pytest.mark.parametrize("phase", ["factory", "feed", "finish"])
@pytest.mark.parametrize("close_failure", [False, True])
async def test_parent_cancellation_keeps_cleanup_once_and_never_returns_prefix(
    phase, close_failure
):
    class Step:
        closed = 0

        def queue_cancel(self):
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)

        def begin_public_text_step(self, *, checkpoint):
            self.queue_cancel()
            return self

        def feed(self, content_id, text, *, checkpoint):
            self.queue_cancel()
            return "not-published"

        def finish(self, content_id, *, checkpoint):
            return self.feed(content_id, "", checkpoint=checkpoint)

        def close(self):
            self.closed += 1
            if close_failure:
                raise RuntimeError("private cleanup diagnostic")

    step = Step()

    async def operation():
        if phase == "factory":
            return await boundary.begin_text_step(step, CancelToken())
        try:
            return await boundary.protect_text(
                step, "content", None if phase == "finish" else "safe", CancelToken()
            )
        finally:
            boundary.close_text_step(step, failed=True)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(operation())
    assert step.closed == 1
