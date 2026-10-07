"""原Gateway的取消结算边界；模拟注入不代表真实Git/Owner故障验收。"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.router import ResolvedAction
from tests.support.agent_preplanning import (
    RecordingPreparer,
    UnrenderablePreparationError,
    preplanning_harness,
)

_PRIVATE_CANARY = "simulated-settlement-canary-/private/tenant/repository"
_PRIVATE_REASON = "simulated_owner_settlement_failed"
_PUBLIC_MESSAGE = "Action计划阶段失败；内部原因不公开"


class SimulatedSettlementFailure(UnrenderablePreparationError):
    """内部原因只供测试检查，公开映射不得渲染此模拟异常。"""

    def __init__(self) -> None:
        super().__init__("simulated_owner_settlement_failed", _PRIVATE_CANARY, retryable=True)
        self.reason = _PRIVATE_REASON


@pytest.mark.parametrize("control", ["token", "task", "deadline", "router_deadline"])
@pytest.mark.parametrize("nested", [False, True], ids=["one-layer", "two-layers"])
async def test_gateway_settlement_failure_is_private_and_blocks_route_review_and_effect(
    tmp_path: Path, control: str, nested: bool, caplog: pytest.LogCaptureFixture
) -> None:
    token, failure = CancelToken(), SimulatedSettlementFailure()
    entered = asyncio.Event()
    operation_tasks: list[asyncio.Task] = []
    cancellations: list[asyncio.CancelledError] = []
    internal_records: list[tuple[KernelError, str]] = []

    async def suspend() -> ResolvedAction:
        task = asyncio.current_task()
        assert task is not None
        operation_tasks.append(task)
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("准备任务不得正常返回")
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise
        finally:
            await asyncio.sleep(0)
            # 模拟内部结算记录只保存在内存，不伪造真实Owner的持久故障证据。
            internal_records.append((failure, failure.reason))
            raise failure

    async def prepare(*_args: object) -> ResolvedAction:
        if nested:
            return await token.run(suspend(), preserve_failure=True)
        return await suspend()

    provider = RecordingPreparer(prepare)
    with preplanning_harness(tmp_path, provider) as h:
        if control == "router_deadline":
            h.router._execute_timeout_seconds = 0.1
        original_tasks = asyncio.all_tasks()
        deadline = asyncio.timeout(None)

        async def invoke() -> object:
            async with deadline:
                return await h.gateway.prepare(h.thread, h.turn, h.call, token)

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if control == "token":
                token.cancel()
            elif control == "task":
                task.cancel()
            elif control == "deadline":
                deadline.reschedule(asyncio.get_running_loop().time())

            with pytest.raises(KernelError) as caught:
                await asyncio.wait_for(task, timeout=5)

            public = caught.value
            assert type(public) is KernelError and public is not failure
            assert public.code == "action_plan_failed"
            assert public.message == _PUBLIC_MESSAGE and public.retryable is False
            assert public.__suppress_context__ and public.__cause__ is None
            assert public.__context__ is failure
            assert not hasattr(public, "reason")
            projected = public.to_failure()
            assert projected.code == "action_plan_failed"
            assert projected.message == _PUBLIC_MESSAGE and projected.retryable is False
            rendered = "\n".join(
                (
                    str(public),
                    repr(public),
                    projected.model_dump_json(),
                    "".join(traceback.format_exception(public)),
                    caplog.text,
                )
            )
            for private in (_PRIVATE_CANARY, _PRIVATE_REASON):
                assert private not in rendered
                for store in (h.plans, h.audit, h.cas):
                    assert private not in "\n".join(store._db.iterdump())

            assert len(internal_records) == 1
            assert internal_records[0][0] is failure and internal_records[0][1] == failure.reason
            assert failure.reason == _PRIVATE_REASON and failure.retryable
            assert len(cancellations) == len(operation_tasks) == 1
            operation = operation_tasks[0]
            assert operation.done() and not operation.cancelled()
            assert operation.exception() is failure
            assert provider.calls == provider.settled == 1 and provider.finished.is_set()
            assert len(provider.observed) == 1 and provider.observed[0][-1] is token
            assert h.trace == ["prepare"]
            assert (h.root / "file.txt").read_text(encoding="utf-8") == "before"
            h.assert_unplanned()
            assert asyncio.all_tasks() <= original_tasks
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("control", ["token", "task", "deadline", "router_deadline"])
async def test_gateway_plain_cancellation_keeps_original_boundary_without_task_leaks(
    tmp_path: Path, control: str
) -> None:
    entered, settled = asyncio.Event(), asyncio.Event()
    operation_tasks: list[asyncio.Task] = []

    async def suspend(*_args: object) -> ResolvedAction:
        task = asyncio.current_task()
        assert task is not None
        operation_tasks.append(task)
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("准备任务不得正常返回")
        finally:
            await asyncio.sleep(0)
            settled.set()

    token, provider = CancelToken(), RecordingPreparer(suspend)
    with preplanning_harness(tmp_path, provider) as h:
        if control == "router_deadline":
            h.router._execute_timeout_seconds = 0.1
        original_tasks = asyncio.all_tasks()
        deadline = asyncio.timeout(None)

        async def invoke() -> object:
            async with deadline:
                return await h.gateway.prepare(h.thread, h.turn, h.call, token)

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if control == "token":
                token.cancel()
            elif control == "task":
                task.cancel()
            elif control == "deadline":
                deadline.reschedule(asyncio.get_running_loop().time())

            expected = {
                "token": TurnCancelled,
                "task": asyncio.CancelledError,
                "deadline": TimeoutError,
                "router_deadline": KernelError,
            }[control]
            with pytest.raises(expected) as caught:
                await asyncio.wait_for(task, timeout=5)
            if control == "router_deadline":
                assert caught.value.code == "action_plan_failed"
                assert caught.value.message == _PUBLIC_MESSAGE and not caught.value.retryable

            assert settled.is_set() and provider.finished.is_set()
            assert provider.calls == provider.settled == len(operation_tasks) == 1
            assert operation_tasks[0].done() and operation_tasks[0].cancelled()
            assert h.trace == ["prepare"]
            assert (h.root / "file.txt").read_text(encoding="utf-8") == "before"
            h.assert_unplanned()
            assert asyncio.all_tasks() <= original_tasks
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


_PRIVATE_REASON_GROUP = ("simulated_receipt_invalid", "simulated_stop_unverifiable")


def assert_fixed_private_settlement_error(
    public: KernelError, marker: SimulatedSettlementFailure, caplog: pytest.LogCaptureFixture
) -> None:
    """检查原Gateway公开投影与异常链，不渲染仅供模拟Owner持有的内部异常。"""

    assert type(public) is KernelError and public is not marker
    assert public.code == "action_plan_failed"
    assert public.message == _PUBLIC_MESSAGE and public.retryable is False
    assert public.__suppress_context__ and public.__cause__ is None
    assert public.__context__ is marker
    assert not hasattr(public, "reason") and not hasattr(public, "reason_group")
    projected = public.to_failure()
    assert projected.code == "action_plan_failed"
    assert projected.message == _PUBLIC_MESSAGE and projected.retryable is False
    rendered = "\n".join(
        (
            str(public),
            repr(public),
            projected.model_dump_json(),
            "".join(traceback.format_exception(public)),
            caplog.text,
        )
    )
    for private in (_PRIVATE_CANARY, _PRIVATE_REASON, *_PRIVATE_REASON_GROUP):
        assert private not in rendered


@pytest.mark.parametrize("control", ["token", "task", "deadline"])
@pytest.mark.parametrize("nested", [False, True], ids=["one-layer", "two-layers"])
async def test_gateway_second_task_cancel_during_drain_keeps_private_owner_failure(
    tmp_path: Path, control: str, nested: bool, caplog: pytest.LogCaptureFixture
) -> None:
    token, marker = CancelToken(), SimulatedSettlementFailure()
    marker.reason_group = _PRIVATE_REASON_GROUP
    inner_token = CancelToken() if control == "token" else token
    entered, settling = asyncio.Event(), asyncio.Event()
    repeated_cancel, finished, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    operation_tasks: list[asyncio.Task] = []
    preparer_tasks: list[asyncio.Task] = []
    cancellations: list[asyncio.CancelledError] = []
    internal_records: list[tuple[KernelError, tuple[str, ...]]] = []

    async def suspend() -> ResolvedAction:
        child = asyncio.current_task()
        assert child is not None
        operation_tasks.append(child)
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("准备任务不得正常返回")
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise
        finally:
            settling.set()
            try:
                await release.wait()
            except asyncio.CancelledError as error:
                cancellations.append(error)
                repeated_cancel.set()
            finally:
                # 只模拟Owner强结算故障，不制造真实Git/Owner的持久故障验收证据。
                internal_records.append((marker, marker.reason_group))
                finished.set()
                raise marker

    async def prepare(*_args: object) -> ResolvedAction:
        child = asyncio.current_task()
        assert child is not None
        preparer_tasks.append(child)
        if nested:
            return await inner_token.run(suspend(), preserve_failure=True)
        return await suspend()

    provider = RecordingPreparer(prepare)
    with preplanning_harness(tmp_path, provider) as h:
        original_tasks = asyncio.all_tasks()
        deadline = asyncio.timeout(None)

        async def invoke() -> object:
            async with deadline:
                return await h.gateway.prepare(h.thread, h.turn, h.call, token)

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if control == "token":
                token.cancel()
            elif control == "task":
                assert task.cancel("simulated-first-task-cancel")
            else:
                deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.wait_for(settling.wait(), timeout=5)
            assert len(cancellations) == 1 and not finished.is_set()
            assert not provider.finished.is_set() and not task.done()
            assert task.cancel("simulated-second-task-cancel")

            with pytest.raises(KernelError) as caught:
                await asyncio.wait_for(task, timeout=5)

            assert_fixed_private_settlement_error(caught.value, marker, caplog)
            for store in (h.plans, h.audit, h.cas):
                stored = "\n".join(store._db.iterdump())
                for private in (_PRIVATE_CANARY, _PRIVATE_REASON, *_PRIVATE_REASON_GROUP):
                    assert private not in stored
            assert len(internal_records) == 1
            assert internal_records[0][0] is marker
            assert internal_records[0][1] is _PRIVATE_REASON_GROUP
            assert marker.reason == _PRIVATE_REASON and marker.retryable
            assert repeated_cancel.is_set() and finished.is_set() and not release.is_set()
            assert len(cancellations) == 2
            assert len(operation_tasks) == len(preparer_tasks) == 1
            assert (operation_tasks[0] is preparer_tasks[0]) == (not nested)
            for child in (*operation_tasks, *preparer_tasks):
                assert child.done() and not child.cancelled() and child.exception() is marker
            assert task.done() and not task.cancelled() and task.exception() is caught.value
            assert provider.calls == provider.settled == 1 and provider.finished.is_set()
            assert len(provider.observed) == 1 and provider.observed[0][-1] is token
            assert h.trace == ["prepare"]
            assert (h.root / "file.txt").read_text(encoding="utf-8") == "before"
            h.assert_unplanned()
            assert asyncio.all_tasks() <= original_tasks
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
