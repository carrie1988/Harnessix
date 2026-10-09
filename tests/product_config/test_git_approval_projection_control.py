"""原审批内存投影分层：完整事件重放、首失败与 foreign 回退；不是认证验收。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.domain.models import ApprovalOutcome
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.product_config.test_git_approval_history_projection import transcript as transcript
from tests.product_config.test_git_layered_projection_control import Trace


@pytest.fixture(params=["pending", "approved", "denied", "cancelled"])
def projection(transcript, request):
    if request.param == "cancelled":
        transcript.cancel()
    elif request.param != "pending":
        transcript.decide(
            ApprovalOutcome.APPROVED if request.param == "approved" else ApprovalOutcome.REJECTED
        )
    trace = Trace()
    expected = transcript.interpret(check=lambda: trace.check("full"))
    assert expected.state == request.param
    return transcript.interpret, expected, trace.counts["full"]


def test_same_task_projection_keeps_all_original_checks_and_exact_facts(projection):
    operation, expected, calls = projection
    trace = Trace()
    assert operation(check=trace.control) == expected
    assert calls >= 4
    assert trace.events == ["full"] + ["local"] * (calls + 1) + ["full"]


@pytest.mark.parametrize(
    "kind", ["semantic-code", "blob-code", "value", "cancel", "timeout", "nested"]
)
@pytest.mark.parametrize("edge", ["entry", "local", "exit"])
def test_first_control_object_is_never_reclassified_or_overridden(projection, kind, edge):
    operation, _, calls = projection
    error = {
        "semantic-code": KernelError("git_approval_history_changed", "original control"),
        "blob-code": KernelError("delivery_blob_corrupt", "original control"),
        "value": ValueError("original control"),
        "cancel": TurnCancelled("original control"),
        "timeout": TimeoutError("original deadline"),
        "nested": UpstreamCheckpointError(UpstreamCheckpointError(ValueError("original"))),
    }[kind]
    trace, result = Trace(), None
    stop = (calls + 1) // 2
    phase, count = ("local", stop) if edge == "local" else ("full", 1 if edge == "entry" else 2)
    trace.failures[phase, count] = error
    if edge == "local":
        trace.drift_at = stop
    with pytest.raises(BaseException) as caught:
        result = operation(check=trace.control)
    assert caught.value is error and result is None
    expected_trace = {
        "entry": ["full"],
        "local": ["full"] + ["local"] * stop,
        "exit": ["full"] + ["local"] * (calls + 1) + ["full"],
    }[edge]
    assert trace.events == expected_trace


def test_exit_authentication_rejects_persistent_drift_without_result(projection):
    operation, _, calls = projection
    trace = Trace()
    trace.drift_at = (calls + 1) // 2
    with pytest.raises(KernelError) as caught:
        operation(check=trace.control)
    assert caught.value is trace.drift_error
    assert trace.events == ["full"] + ["local"] * (calls + 1) + ["full"]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_callbacks_keep_original_full_trajectory(projection, kind):
    operation, expected, calls = projection
    trace = Trace()

    def forbidden_pure():
        pytest.fail("unknown callback must not borrow a pure segment")

    def full():
        trace.check("full")

    full.pure = forbidden_pure

    class Proxy:
        pure = staticmethod(forbidden_pure)

        def __call__(self):
            full()

    class Subclass(GitAuthenticationControl):
        pure = staticmethod(forbidden_pure)

    control = {
        "function": full,
        "proxy": Proxy(),
        "subclass": Subclass(lambda: pytest.fail("no local borrowing"), full),
    }[kind]
    assert operation(check=control) == expected
    assert trace.events == ["full"] * calls


@pytest.mark.parametrize("surface", ["task", "thread"])
async def test_foreign_execution_uses_full_fallback_at_every_original_check(projection, surface):
    operation, expected, calls = projection
    trace = Trace()

    async def child():
        return operation(check=trace.control)

    result = (
        await asyncio.create_task(child())
        if surface == "task"
        else await asyncio.to_thread(operation, check=trace.control)
    )
    assert result == expected
    assert trace.events == ["full"] * (calls + 2)


@pytest.mark.parametrize("surface", ["same-task", "foreign-task", "foreign-thread"])
@pytest.mark.parametrize("override", ["no-op", "borrowed-local"])
async def test_instance_pure_override_cannot_skip_authentication_or_lend_local(
    projection, surface, override
):
    operation, expected, calls = projection
    trace, dispatch = Trace(), []

    @contextmanager
    def forged_pure():
        dispatch.append(override)
        yield (lambda: trace.check("local")) if override == "borrowed-local" else (lambda: None)

    trace.control.pure = forged_pure

    async def child():
        return operation(check=trace.control)

    if surface == "same-task":
        result = operation(check=trace.control)
        expected_trace = ["full"] + ["local"] * (calls + 1) + ["full"]
    elif surface == "foreign-task":
        result = await asyncio.create_task(child())
        expected_trace = ["full"] * (calls + 2)
    else:
        result = await asyncio.to_thread(operation, check=trace.control)
        expected_trace = ["full"] * (calls + 2)
    assert result == expected and dispatch == [] and trace.events == expected_trace
