"""公开保护的调度与工作期限分离；真实扫描仍有原10秒上限，排队取消仍须交付。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from harnessix.agent import publication as boundary
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.input_publication import protect_input


class Guard:
    """无凭据的纯检查端口，只用于核对原扫描确实发生而非被跳过。"""

    def __init__(self, operation=lambda: None):
        self.operation = operation
        self.calls = 0

    def assert_public_json(self, value, *, checkpoint):
        checkpoint()
        self.calls += 1
        self.operation()
        checkpoint()

    def assert_public_jsonl(self, body, *, checkpoint):
        self.assert_public_json(body, checkpoint=checkpoint)


async def scan(kind, guard, cancel):
    if kind == "input":
        await protect_input(guard, {"request": "safe"}, cancel)
    elif kind == "json":
        await boundary.protect_json(guard, {"response": "safe"}, cancel)
    else:
        await boundary.protect_jsonl(guard, b'{"record":"safe"}\n', cancel)


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
@pytest.mark.parametrize("handoff", [1, 2])
async def test_unrelated_task_wait_is_not_synchronous_scan_work(monkeypatch, kind, handoff):
    """只调整保护边界的时钟，其他Task耗时不能吃掉未开始或已完成的扫描预算。"""
    clock = [boundary.monotonic()]
    original_sleep = asyncio.sleep
    handoffs = []

    async def queued(delay):
        if sys._getframe(1).f_code.co_name == "_protect":
            handoffs.append(delay)
            if len(handoffs) == handoff:
                clock[0] += boundary.PUBLIC_PROTECTION_TIMEOUT + 1
        await original_sleep(delay)

    guard = Guard()
    with monkeypatch.context() as patch:
        patch.setattr(boundary, "monotonic", lambda: clock[0])
        patch.setattr(asyncio, "sleep", queued)
        await scan(kind, guard, CancelToken())
    assert guard.calls == 1
    assert handoffs == [0, 0], "两端取消交接不能被删除以规避调度问题"


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
async def test_actual_synchronous_scan_still_uses_original_deadline(monkeypatch, kind):
    clock = [boundary.monotonic()]
    monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])

    def slow():
        clock[0] += boundary.PUBLIC_PROTECTION_TIMEOUT + 1

    guard = Guard(slow)
    with pytest.raises(KernelError) as caught:
        await scan(kind, guard, CancelToken())
    assert guard.calls == 1
    assert caught.value.code == (
        "public_input_timeout" if kind == "input" else "public_output_timeout"
    )


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
@pytest.mark.parametrize("cancellation", ["token", "parent"])
async def test_cancellation_queued_during_scan_is_not_published(kind, cancellation):
    token = CancelToken()

    async def operation():
        task = asyncio.current_task()
        assert task is not None
        cancel = token.cancel if cancellation == "token" else task.cancel
        guard = Guard(lambda: asyncio.get_running_loop().call_soon(cancel))
        await scan(kind, guard, token)

    expected = TurnCancelled if cancellation == "token" else asyncio.CancelledError
    with pytest.raises(expected):
        await asyncio.create_task(operation())


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
async def test_uncooperative_scan_cannot_bypass_completion_deadline(monkeypatch, kind):
    """端口完全不消费检查点时，完成后的期限核对仍必须拒绝结果。"""
    clock = [boundary.monotonic()]
    monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])

    class Uncooperative(Guard):
        def assert_public_json(self, value, *, checkpoint):
            self.calls += 1
            clock[0] += boundary.PUBLIC_PROTECTION_TIMEOUT + 1

    guard = Uncooperative()
    with pytest.raises(KernelError) as caught:
        await scan(kind, guard, CancelToken())
    assert guard.calls == 1
    assert caught.value.code == (
        "public_input_timeout" if kind == "input" else "public_output_timeout"
    )


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
async def test_existing_parent_cancellation_never_starts_scan(kind):
    guard = Guard()

    async def operation():
        asyncio.current_task().cancel()
        await scan(kind, guard, CancelToken())

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(operation())
    assert guard.calls == 0


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
async def test_outer_timeout_remains_outer_cancellation(kind):
    guard = Guard()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await scan(kind, guard, CancelToken())
    assert guard.calls == 0


@pytest.mark.parametrize("kind", ["input", "json", "jsonl"])
@pytest.mark.parametrize("error", [TimeoutError, RuntimeError])
async def test_original_scan_failure_is_not_replaced_by_tail_handoff(kind, error):
    handoffs = []

    def fail():
        asyncio.get_running_loop().call_soon(handoffs.append, "queued")
        raise error("private guard diagnostic must not escape")

    with pytest.raises(KernelError) as caught:
        await scan(kind, Guard(fail), CancelToken())
    assert caught.value.code == (
        "public_input_protection_failed" if kind == "input" else "public_output_protection_failed"
    )
    assert "private guard diagnostic" not in str(caught.value)
    assert handoffs == [], "扫描首失败不能被新增finally中的调度交接覆盖"


async def test_step_factory_tail_cancellation_closes_created_step_once():
    token = CancelToken()

    class Step:
        closed = 0

        def feed(self, content_id, text, *, checkpoint):
            return text

        def finish(self, content_id, *, checkpoint):
            return ""

        def close(self):
            self.closed += 1

    step = Step()

    class Factory:
        def begin_public_text_step(self, *, checkpoint):
            asyncio.get_running_loop().call_soon(token.cancel)
            return step

    with pytest.raises(TurnCancelled):
        await boundary.begin_text_step(Factory(), token)
    assert step.closed == 1


@pytest.mark.parametrize("finish", [False, True])
async def test_text_tail_cancellation_returns_no_prefix_and_keeps_caller_cleanup(finish):
    token = CancelToken()

    class Step:
        closed = 0

        def feed(self, content_id, text, *, checkpoint):
            asyncio.get_running_loop().call_soon(token.cancel)
            return "not-published"

        def finish(self, content_id, *, checkpoint):
            return self.feed(content_id, "", checkpoint=checkpoint)

        def close(self):
            self.closed += 1

    step = Step()
    try:
        with pytest.raises(TurnCancelled):
            await boundary.protect_text(step, "content", None if finish else "safe", token)
    finally:
        boundary.close_text_step(step, failed=True)
    assert step.closed == 1
