"""取消收尾契约：结算故障为模拟注入，不代表真实Owner故障验收。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError


class SimulatedSettlementFailure(KernelError):
    """携带内部原因的模拟结算异常，必须以同一对象传播。"""

    def __init__(self) -> None:
        super().__init__("simulated_settlement_failed", "模拟结算失败", retryable=True)
        self.reason = "simulated_finally_settlement_failed"


class SuspendedOperation:
    """等待取消后执行异步finally，并可注入结算异常。"""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.cancellations: list[asyncio.CancelledError] = []

    async def run(self) -> None:
        self.task = asyncio.current_task()
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            self.cancellations.append(error)
            raise
        finally:
            await asyncio.sleep(0)
            self.finished.set()
            if self.failure is not None:
                raise self.failure


async def cancel_when_started(
    operation: Awaitable[object], entered: asyncio.Event, token: CancelToken, control: str
) -> object:
    """通过真实Token、Task或可重排绝对期限取消已进入的子任务。"""

    deadline = asyncio.timeout(None)

    async def invoke() -> object:
        async with deadline:
            return await operation

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if control == "token":
            token.cancel()
        elif control == "task":
            task.cancel()
        else:
            assert control == "deadline"
            deadline.reschedule(asyncio.get_running_loop().time())
        return await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def cancellation_type(control: str) -> type[BaseException]:
    return {
        "token": TurnCancelled,
        "task": asyncio.CancelledError,
        "deadline": TimeoutError,
    }[control]


def test_preserve_failure_is_keyword_only_and_disabled_by_default() -> None:
    parameter = inspect.signature(CancelToken.run).parameters["preserve_failure"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
@pytest.mark.parametrize("explicit_default", [False, True], ids=["omitted", "explicit-false"])
async def test_default_false_keeps_cancellation_when_finally_fails(
    control: str, explicit_default: bool
) -> None:
    original_tasks = asyncio.all_tasks()
    token, failure = CancelToken(), SimulatedSettlementFailure()
    operation = SuspendedOperation(failure)
    pending = (
        token.run(operation.run(), preserve_failure=False)
        if explicit_default
        else token.run(operation.run())
    )

    with pytest.raises(cancellation_type(control)) as caught:
        await cancel_when_started(pending, operation.entered, token, control)

    assert caught.value is not failure
    assert operation.finished.is_set() and len(operation.cancellations) == 1
    assert operation.task is not None and operation.task.done()
    assert not operation.task.cancelled() and operation.task.exception() is failure
    assert asyncio.all_tasks() <= original_tasks


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
async def test_preserve_failure_keeps_finally_error_identity_and_reason(control: str) -> None:
    original_tasks = asyncio.all_tasks()
    token, failure = CancelToken(), SimulatedSettlementFailure()
    operation = SuspendedOperation(failure)

    with pytest.raises(SimulatedSettlementFailure) as caught:
        await cancel_when_started(
            token.run(operation.run(), preserve_failure=True), operation.entered, token, control
        )

    assert caught.value is failure
    assert caught.value.reason == "simulated_finally_settlement_failed"
    assert caught.value.code == "simulated_settlement_failed" and caught.value.retryable
    assert operation.finished.is_set() and len(operation.cancellations) == 1
    assert operation.task is not None and operation.task.done()
    assert not operation.task.cancelled() and operation.task.exception() is failure
    assert asyncio.all_tasks() <= original_tasks


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
@pytest.mark.parametrize("preserve_failure", [False, True])
async def test_plain_cancellation_propagates_and_reaps_all_children(
    control: str, preserve_failure: bool
) -> None:
    original_tasks = asyncio.all_tasks()
    token, operation = CancelToken(), SuspendedOperation()

    with pytest.raises(cancellation_type(control)):
        await cancel_when_started(
            token.run(operation.run(), preserve_failure=preserve_failure),
            operation.entered,
            token,
            control,
        )

    assert operation.finished.is_set() and len(operation.cancellations) == 1
    assert operation.task is not None and operation.task.done() and operation.task.cancelled()
    assert asyncio.all_tasks() <= original_tasks


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
async def test_nested_preserve_failure_does_not_swallow_inner_settlement(control: str) -> None:
    original_tasks = asyncio.all_tasks()
    token, failure = CancelToken(), SimulatedSettlementFailure()
    operation = SuspendedOperation(failure)
    inner_task: asyncio.Task | None = None

    async def inner() -> None:
        nonlocal inner_task
        inner_task = asyncio.current_task()
        await token.run(operation.run(), preserve_failure=True)

    with pytest.raises(SimulatedSettlementFailure) as caught:
        await cancel_when_started(
            token.run(inner(), preserve_failure=True), operation.entered, token, control
        )

    assert caught.value is failure and caught.value.reason == "simulated_finally_settlement_failed"
    assert operation.finished.is_set() and len(operation.cancellations) == 1
    for task in (operation.task, inner_task):
        assert task is not None and task.done() and not task.cancelled()
        assert task.exception() is failure
    assert asyncio.all_tasks() <= original_tasks


@pytest.mark.parametrize("preserve_failure", [False, True])
async def test_normal_result_keeps_identity_and_reaps_token_waiter(preserve_failure: bool) -> None:
    original_tasks = asyncio.all_tasks()
    result = object()

    async def complete() -> object:
        return result

    assert await CancelToken().run(complete(), preserve_failure=preserve_failure) is result
    assert asyncio.all_tasks() <= original_tasks


@pytest.mark.parametrize("preserve_failure", [False, True])
async def test_uncancelled_failure_keeps_identity_and_reaps_token_waiter(
    preserve_failure: bool,
) -> None:
    original_tasks = asyncio.all_tasks()
    failure = RuntimeError("模拟普通子任务故障")

    async def reject() -> None:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await CancelToken().run(reject(), preserve_failure=preserve_failure)

    assert caught.value is failure
    assert asyncio.all_tasks() <= original_tasks


class RepeatedCancellationOperation(SuspendedOperation):
    """只模拟Owner排空中再收取消；事件门确保结算异常发生在第二次取消后。"""

    def __init__(self, failure: Exception) -> None:
        super().__init__(failure)
        self.settling = asyncio.Event()
        self.repeated_cancel = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        self.task = asyncio.current_task()
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            self.cancellations.append(error)
            raise
        finally:
            self.settling.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError as error:
                self.cancellations.append(error)
                self.repeated_cancel.set()
            finally:
                self.finished.set()
                assert self.failure is not None
                raise self.failure


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
@pytest.mark.parametrize("nested", [False, True], ids=["one-layer", "two-layers"])
async def test_second_task_cancel_during_drain_preserves_simulated_owner_failure(
    control: str, nested: bool
) -> None:
    original_tasks = asyncio.all_tasks()
    token, marker = CancelToken(), SimulatedSettlementFailure()
    reason_group = ("simulated_receipt_invalid", "simulated_stop_unverifiable")
    marker.reason_group = reason_group
    operation = RepeatedCancellationOperation(marker)
    inner_token = CancelToken() if control == "token" else token
    inner_tasks: list[asyncio.Task] = []
    deadline = asyncio.timeout(None)

    async def inner() -> None:
        child = asyncio.current_task()
        assert child is not None
        inner_tasks.append(child)
        # Token取消隔离内层等待器以定位第二次Task取消；Task/期限取消复用同一Token。
        await inner_token.run(operation.run(), preserve_failure=True)

    async def invoke() -> None:
        async with deadline:
            await token.run(inner() if nested else operation.run(), preserve_failure=True)

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=5)
        if control == "token":
            token.cancel()
        elif control == "task":
            assert task.cancel("simulated-first-task-cancel")
        else:
            deadline.reschedule(asyncio.get_running_loop().time())
        await asyncio.wait_for(operation.settling.wait(), timeout=5)
        assert len(operation.cancellations) == 1
        assert not operation.finished.is_set() and not task.done()
        assert task.cancel("simulated-second-task-cancel")

        with pytest.raises(SimulatedSettlementFailure) as caught:
            await asyncio.wait_for(task, timeout=5)

        assert caught.value is marker
        assert caught.value.reason_group is reason_group
        assert caught.value.reason == "simulated_finally_settlement_failed"
        assert caught.value.code == "simulated_settlement_failed" and caught.value.retryable
        assert operation.repeated_cancel.is_set() and operation.finished.is_set()
        assert len(operation.cancellations) == 2 and not operation.release.is_set()
        assert len(inner_tasks) == int(nested)
        for child in (task, operation.task, *inner_tasks):
            assert child is not None and child.done() and not child.cancelled()
            assert child.exception() is marker
        assert asyncio.all_tasks() <= original_tasks
    finally:
        operation.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
@pytest.mark.parametrize("nested", [False, True], ids=["one-layer", "two-layers"])
@pytest.mark.parametrize("preserve_failure", [False, True])
async def test_child_turn_cancelled_does_not_replace_outer_cancellation(
    control: str, nested: bool, preserve_failure: bool
) -> None:
    original_tasks = asyncio.all_tasks()
    token, marker = CancelToken(), TurnCancelled("simulated-child-domain-cancel")
    operation = SuspendedOperation(marker)
    inner_token = CancelToken() if control == "token" else token
    inner_tasks: list[asyncio.Task] = []
    escaping: list[BaseException] = []
    deadline = asyncio.timeout(None)

    async def inner() -> None:
        child = asyncio.current_task()
        assert child is not None
        inner_tasks.append(child)
        await inner_token.run(operation.run(), preserve_failure=preserve_failure)

    async def invoke() -> None:
        async with deadline:
            try:
                await token.run(
                    inner() if nested else operation.run(), preserve_failure=preserve_failure
                )
            except BaseException as error:
                escaping.append(error)
                raise

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=5)
        if control == "token":
            token.cancel()
        elif control == "task":
            assert task.cancel("simulated-external-task-cancel")
        else:
            deadline.reschedule(asyncio.get_running_loop().time())

        with pytest.raises(cancellation_type(control)) as caught:
            await asyncio.wait_for(task, timeout=5)

        assert type(caught.value) is cancellation_type(control) and caught.value is not marker
        assert len(escaping) == 1
        if control == "deadline":
            assert type(escaping[0]) is asyncio.CancelledError
            assert caught.value.__cause__ is escaping[0] and deadline.expired()
        else:
            assert caught.value is escaping[0]
            if control == "task":
                assert caught.value.args == ("simulated-external-task-cancel",)
        assert escaping[0] is not marker
        assert operation.finished.is_set() and len(operation.cancellations) == 1
        assert operation.task is not None and operation.task.done()
        assert not operation.task.cancelled() and operation.task.exception() is marker
        assert len(inner_tasks) == int(nested)
        assert all(child.done() and child.cancelled() for child in inner_tasks)
        assert task.done() and task.cancelled() == (control == "task")
        assert asyncio.all_tasks() <= original_tasks
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
