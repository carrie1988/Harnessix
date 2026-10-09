"""离线验证已知用量门槛：工具提案取得执行权之前保持原预算与停止边界。"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harnessix.agent import runtime as runtime_module
from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure
from harnessix.agent.models import (
    Budget,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_rejections import require_closed_rejections
from harnessix.agent.usage import ModelAttemptFinished, ModelUsageObserved
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.router import TrustedActionRouter
from tests.agent.attempt_helpers import attempt_start, observed
from tests.agent.helpers import answer
from tests.agent.test_runtime import assert_settled
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.deadlines import capture_deadlines
from tests.trusted_actions.test_agent_gateway import FakeExecutor, build_gateway


@dataclass
class ActionHarness:
    root: Path
    store: SQLiteSessionStore
    gateway: RouterBackedAgentActionGateway
    router: TrustedActionRouter
    executor: FakeExecutor
    prepare: AsyncMock
    execute: AsyncMock


@pytest.fixture
def action_harness(tmp_path: Path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, router, plans, audit = build_gateway(root, executor)
    prepare = AsyncMock(wraps=gateway.prepare)
    execute = AsyncMock(wraps=gateway.execute)
    monkeypatch.setattr(gateway, "prepare", prepare)
    monkeypatch.setattr(gateway, "execute", execute)
    try:
        yield ActionHarness(
            root,
            SQLiteSessionStore(tmp_path / "session.db"),
            gateway,
            router,
            executor,
            prepare,
            execute,
        )
    finally:
        plans.close()
        audit.close()


def write_step(usage: Usage) -> list[ProviderEvent]:
    events = action_step()
    events[-1] = ResponseCompleted(finish_reason="tool_calls", usage=usage)
    return events


def assert_not_dispatched(harness: ActionHarness, turn: Turn) -> None:
    assert harness.prepare.await_count == harness.execute.await_count == 0
    assert harness.executor.calls == 0
    assert not any(
        isinstance(item.content, ToolCallContent | TrustedActionApprovalRequestContent)
        for item in turn.items
    )
    assert_settled(turn)


@pytest.mark.parametrize(
    ("input_tokens", "include_rejection"),
    [(101, False), (100, False), (100, True)],
    ids=["over-limit", "at-limit", "mixed-at-limit"],
)
async def test_registered_write_proposal_at_token_boundary(
    action_harness: ActionHarness, input_tokens: int, include_rejection: bool, record_property
) -> None:
    harness = action_harness
    events = write_step(Usage(input_tokens=input_tokens))
    if include_rejection:
        events.insert(1, ToolCallRejected(call_id="rejected-peer", argument_chars=0))
    provider = ScriptedProvider([events])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline token boundary",
            request_id="boundary",
            budget=Budget(max_tokens=100),
        )

    record_property("turn_status", turn.status.value)
    record_property("failure_code", turn.error.code if turn.error else None)
    record_property("known_tokens", turn.usage.total_tokens)
    record_property("gateway_prepare", harness.prepare.await_count)
    record_property("gateway_execute", harness.execute.await_count)
    record_property(
        "executable_calls", sum(isinstance(item.content, ToolCallContent) for item in turn.items)
    )
    assert turn.usage == Usage(input_tokens=input_tokens)
    assert harness.prepare.await_count == harness.execute.await_count == 0
    assert harness.executor.calls == 0
    assert provider.closed_streams == len(provider.requests) == 1
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "budget_exceeded"
    assert_not_dispatched(harness, turn)
    assert not any(isinstance(item.content, ToolCallRejectionContent) for item in turn.items)
    assert not pending_calls(turn)
    assert replay(await harness.store.events(thread.thread_id)) == await harness.store.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("last_input_tokens", [40, 41], ids=["at-limit", "over-limit"])
async def test_cumulative_retry_usage_blocks_registered_write(
    action_harness: ActionHarness, last_input_tokens: int
) -> None:
    harness = action_harness
    first, second = attempt_start(), attempt_start(index=2)
    step = write_step(Usage(input_tokens=last_input_tokens))
    step[0] = ResponseStarted(response_id="response-2")
    events = [
        first,
        observed(first, completeness="complete", input_tokens=60, output_tokens=0),
        ModelAttemptFinished(
            attempt_id=first.attempt_id,
            outcome="failed",
            error=AgentFailure(code="provider_transport", message="离线已知失败", retryable=True),
        ),
        second,
        *step[:-1],
        observed(second, completeness="complete", input_tokens=last_input_tokens, output_tokens=0),
        ModelAttemptFinished(attempt_id=second.attempt_id, outcome="completed"),
        step[-1],
    ]
    provider = ScriptedProvider([events])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline retry accounting",
            request_id="attempts",
            budget=Budget(max_tokens=100),
        )

    assert turn.usage == Usage(input_tokens=60 + last_input_tokens)
    assert turn.usage_is_complete and turn.usage_step == 1
    assert [attempt.status for attempt in turn.model_attempts] == ["failed", "completed"]
    assert turn.model_attempts[-1].usage.input_tokens == last_input_tokens
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "budget_exceeded"
    assert_not_dispatched(harness, turn)
    assert provider.closed_streams == len(provider.requests) == 1
    assert replay(await harness.store.events(thread.thread_id)) == await harness.store.get_thread(
        thread.thread_id
    )


async def test_below_boundary_keeps_original_action_approval(
    action_harness: ActionHarness,
) -> None:
    harness = action_harness
    provider = ScriptedProvider([write_step(Usage(input_tokens=99)), answer()])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        waiting = await runtime.run_turn(
            thread.thread_id,
            "offline approval",
            request_id="below-boundary",
            budget=Budget(max_tokens=100),
        )
        request = approval(waiting)
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert harness.prepare.await_count == 1 and harness.execute.await_count == 0
        assert harness.executor.calls == 0 and waiting.usage.total_tokens == 99
        assert harness.router.status(request.plan_id).state == "pending_approval"
        await runtime.reply_approval(
            thread.thread_id,
            waiting.turn_id,
            request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="offline-reviewer"),
        )
        completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)

    assert completed.status is TurnStatus.COMPLETED and completed.error is None
    assert completed.usage == Usage(input_tokens=99)
    assert harness.prepare.await_count == harness.execute.await_count == harness.executor.calls == 1
    assert len(provider.requests) == provider.closed_streams == 2
    assert_settled(completed)


async def test_exact_budget_text_only_response_still_completes(
    action_harness: ActionHarness,
) -> None:
    harness = action_harness
    events = answer()
    events[-1] = ResponseCompleted(usage=Usage(input_tokens=100))
    provider = ScriptedProvider([events])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline final text",
            request_id="text-only",
            budget=Budget(max_tokens=100),
        )

    assert turn.status is TurnStatus.COMPLETED and turn.error is None
    assert turn.usage == Usage(input_tokens=100)
    assert_not_dispatched(harness, turn)
    assert provider.closed_streams == len(provider.requests) == 1


@pytest.mark.parametrize("proposal_kind", ["rejected", "unregistered"])
async def test_rejection_only_exact_budget_preserves_audit(
    action_harness: ActionHarness, proposal_kind: str
) -> None:
    harness = action_harness
    proposal = (
        ToolCallRejected(call_id="rejected", argument_chars=0)
        if proposal_kind == "rejected"
        else ToolCallCompleted(call_id="rejected", tool="unregistered.write")
    )
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="rejection-response"),
                proposal,
                ResponseCompleted(finish_reason="tool_calls", usage=Usage(input_tokens=100)),
            ]
        ]
    )
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline rejection audit",
            request_id="rejection-only",
            budget=Budget(max_tokens=100),
        )

    rejections = [
        item.content for item in turn.items if isinstance(item.content, ToolCallRejectionContent)
    ]
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(rejections) == len(results) == 1
    assert rejections[0].reason == "unregistered_tool" and rejections[0].model_step == 1
    assert results[0].call_id == rejections[0].call_id
    assert results[0].outcome == "failed"
    assert results[0].error is not None and results[0].error.code == "unknown_tool"
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "budget_exceeded"
    assert turn.usage == Usage(input_tokens=100)
    assert_not_dispatched(harness, turn)
    snapshot = await harness.store.get_thread(thread.thread_id)
    require_closed_rejections(snapshot)
    assert replay(await harness.store.events(thread.thread_id)) == snapshot


async def test_actually_dispatched_unknown_remains_conservative(
    action_harness: ActionHarness,
) -> None:
    harness = action_harness
    harness.executor.outcome = ActionExecutionOutcome(kind="unknown", error_code="executor_unknown")
    provider = ScriptedProvider([write_step(Usage(input_tokens=1))])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        waiting = await runtime.run_turn(
            thread.thread_id,
            "offline actual unknown",
            request_id="executed-unknown",
            budget=Budget(max_tokens=100),
        )
        request = approval(waiting)
        await runtime.reply_approval(
            thread.thread_id,
            waiting.turn_id,
            request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="offline-reviewer"),
        )
        turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)

    assert harness.prepare.await_count == harness.execute.await_count == harness.executor.calls == 1
    assert turn.status is TurnStatus.INTERRUPTED
    assert turn.error is not None and turn.error.code == "uncertain_effect"
    result = next(
        item.content for item in turn.items if isinstance(item.content, ToolResultContent)
    )
    assert result.outcome == "unknown" and result.action_id == request.plan_id
    assert result.trusted_action is not None and result.trusted_action.state == "unknown"
    assert harness.router.status(request.plan_id).state == "unknown"
    assert_settled(turn)
    assert len(provider.requests) == provider.closed_streams == 1


async def test_prepare_crash_before_session_approval_keeps_uncertain_write(
    action_harness: ActionHarness, monkeypatch
) -> None:
    """Router已形成计划但Session尚无审批时，不把未执行误判成安全无效果。"""
    harness = action_harness
    real_prepare = harness.gateway.prepare

    async def crash_after_prepare(*args, **kwargs):
        prepared = await real_prepare(*args, **kwargs)
        assert isinstance(prepared, TrustedActionApprovalRequestContent)
        raise RuntimeError("injected before Session approval")

    monkeypatch.setattr(harness.gateway, "prepare", crash_after_prepare)
    provider = ScriptedProvider([write_step(Usage(input_tokens=1))])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline prepare crash",
            request_id="prepare-crash",
            budget=Budget(max_tokens=100),
        )

    assert harness.prepare.await_count == 1
    assert harness.execute.await_count == harness.executor.calls == 0
    assert turn.status is TurnStatus.INTERRUPTED
    assert turn.error is not None and turn.error.code == "uncertain_effect"
    assert turn.usage == Usage(input_tokens=1)
    calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(calls) == len(results) == 1
    assert results[0].call_id == calls[0].call_id and results[0].outcome == "unknown"
    assert not any(
        isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
    )
    plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, calls[0])
    assert harness.router.status(plan_id).state == "pending_approval"
    assert_settled(turn)
    assert len(provider.requests) == provider.closed_streams == 1
    assert replay(await harness.store.events(thread.thread_id)) == await harness.store.get_thread(
        thread.thread_id
    )


def unfinished_attempt_write() -> list[ProviderEvent]:
    start = attempt_start()
    step = write_step(Usage(input_tokens=101))
    step[0] = ResponseStarted(response_id="response-1")
    return [
        start,
        *step[:-1],
        observed(start, completeness="complete", input_tokens=101, output_tokens=0),
    ]


async def test_incomplete_stream_keeps_provider_failure_and_known_usage(
    action_harness: ActionHarness,
) -> None:
    harness = action_harness
    provider = ScriptedProvider([unfinished_attempt_write()])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline incomplete stream",
            request_id="incomplete",
            budget=Budget(max_tokens=100),
        )

    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "provider_stream_incomplete"
    assert turn.usage == Usage(input_tokens=101)
    assert turn.model_attempts[0].status == "failed"
    assert_not_dispatched(harness, turn)
    assert len(provider.requests) == provider.closed_streams == 1


async def test_malformed_tail_takes_priority_over_sampled_token_budget(
    action_harness: ActionHarness,
) -> None:
    harness = action_harness
    events = write_step(Usage(input_tokens=101))
    events.append(ResponseStarted(response_id="invalid-after-terminal"))
    provider = ScriptedProvider([events])
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        turn = await runtime.run_turn(
            thread.thread_id,
            "offline malformed tail",
            request_id="malformed-tail",
            budget=Budget(max_tokens=100),
        )

    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "invalid_provider_output"
    assert turn.usage == Usage(input_tokens=101)
    assert_not_dispatched(harness, turn)
    assert len(provider.requests) == provider.closed_streams == 1


class PausedAfterUsageProvider(ScriptedProvider):
    """在已知用量落账后阻塞流尾，让原取消与期限入口决定终态。"""

    def __init__(self) -> None:
        super().__init__([unfinished_attempt_write()])
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: ModelRequest, cancel: CancelToken):
        async with aclosing(super().stream(request, cancel)) as events:
            async for event in events:
                yield event
                if isinstance(event, ModelUsageObserved):
                    self.waiting.set()
                    await cancel.run(self.release.wait())


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_stop_before_stream_close_preserves_known_usage_and_no_dispatch(
    action_harness: ActionHarness, monkeypatch, stop: str
) -> None:
    harness = action_harness
    provider = PausedAfterUsageProvider()
    deadlines = capture_deadlines(monkeypatch, runtime_module)
    async with AgentRuntime(harness.store, provider, trusted_actions=harness.gateway) as runtime:
        thread = await runtime.create_thread(str(harness.root))
        task = asyncio.create_task(
            runtime.run_turn(
                thread.thread_id,
                "offline stop conservation",
                request_id="stopped-stream",
                budget=Budget(max_tokens=100),
            )
        )
        await asyncio.wait_for(provider.waiting.wait(), 5)
        if stop == "cancel":
            active = (await harness.store.get_thread(thread.thread_id)).active_turn_id
            assert active is not None
            await runtime.cancel(thread.thread_id, active)
        else:
            deadlines[-1].reschedule(asyncio.get_running_loop().time())
        turn = await asyncio.wait_for(task, 5)

    assert turn.status is (TurnStatus.CANCELLED if stop == "cancel" else TurnStatus.FAILED)
    assert turn.error is not None
    assert turn.error.code == ("cancelled" if stop == "cancel" else "time_budget_exceeded")
    assert turn.usage == Usage(input_tokens=101)
    assert all(attempt.status != "running" for attempt in turn.model_attempts)
    assert_not_dispatched(harness, turn)
    assert len(provider.requests) == provider.closed_streams == 1
    assert replay(await harness.store.events(thread.thread_id)) == await harness.store.get_thread(
        thread.thread_id
    )
