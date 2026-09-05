from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    Principal,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.models._history import messages_for
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.contracts import ProcessLimits
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.worker import ActionWorker
from tests.agent.helpers import answer


def _process_step(code: str, *arguments: str) -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="process-response"),
        ToolCallCompleted(
            call_id="process-call",
            tool="host.process",
            arguments={
                "program": "python",
                "arguments": ["-I", "-c", code, *arguments],
                "timeout_seconds": 5.0,
            },
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def _service_and_bridge(
    tmp_path: Path, *, limits: ProcessLimits | None = None
) -> tuple[ActionService, ProcessAgentBridge]:
    registry = ToolRegistry()
    registry.register(
        process_action_tool(
            lambda: HostProcessRuntime(
                tmp_path,
                {"python": sys.executable},
                limits=limits,
            )
        )
    )
    service = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await service.initialize()
    bridge = ProcessAgentBridge(
        service,
        Principal(
            tenant_id="tenant-a",
            subject_id="agent-a",
            framework="harnessix-agent",
            roles=("developer",),
        ),
    )
    return service, bridge


def _approval(turn: Turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, ProcessApprovalRequestContent)
    )


async def test_process_agent_uses_external_worker_and_bounded_observation(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    provider = ScriptedProvider(
        [
            _process_step(
                "import sys; open(sys.argv[1], 'w').write('once'); print('private-output')",
                str(marker),
            ),
            answer("进程结果已处理"),
        ]
    )
    service, bridge = await _service_and_bridge(tmp_path)
    store = SQLiteSessionStore(tmp_path / "session.db")
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            pending = await runtime.run_turn(thread.thread_id, "执行任务", request_id="process-1")
            approval = _approval(pending)
            assert pending.status is TurnStatus.WAITING_APPROVAL
            assert not marker.exists()
            assert {tool.name for tool in provider.requests[0].tools} == {"host.process"}
            action = await service.get(approval.plan.action_id)
            assert action.status is ActionStatus.PENDING_APPROVAL

            waiting = await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="reviewer-a",
                    reason="允许固定程序",
                ),
            )
            assert waiting.status is TurnStatus.WAITING_ACTION
            assert not marker.exists()

            observed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert observed.status is TurnStatus.WAITING_ACTION
            sequence = (await store.get_thread(thread.thread_id)).sequence
            same = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert same.status is TurnStatus.WAITING_ACTION
            assert (await store.get_thread(thread.thread_id)).sequence == sequence

        completed_action = await ActionWorker(
            service,
            poll_seconds=0.01,
            heartbeat_seconds=1,
            recovery_interval_seconds=1,
        ).run_once()
        assert completed_action is not None
        assert completed_action.status is ActionStatus.SUCCEEDED
        assert marker.read_text() == "once"
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert completed.status is TurnStatus.COMPLETED
    finally:
        await service.close()

    states = [
        item.content
        for item in completed.items
        if isinstance(item.content, ProcessActionStateContent)
    ]
    assert [state.effect.status for state in states] == [ActionStatus.READY, ActionStatus.SUCCEEDED]
    result = next(
        item.content for item in completed.items if isinstance(item.content, ToolResultContent)
    )
    assert result.outcome == "succeeded" and result.process == states[-1].effect
    assert isinstance(result.output, dict)
    stdout = result.output["stdout"]
    assert isinstance(stdout, dict)
    assert stdout["observed_bytes"] == len(b"private-output\n")
    assert "data_base64" not in json.dumps(result.output)
    messages = messages_for(provider.requests[1])
    tool_message = json.loads(
        next(message["content"] for message in messages if message["role"] == "tool")
    )
    assert set(tool_message) == {"outcome", "output", "error"}


async def test_process_decision_retry_and_session_sync_are_idempotent(tmp_path: Path) -> None:
    provider = ScriptedProvider([_process_step("print('not-run')"), answer()])
    service, bridge = await _service_and_bridge(tmp_path)
    store = SQLiteSessionStore(tmp_path / "session.db")
    failed_once = False

    def fault(name: str) -> None:
        nonlocal failed_once
        if name == "runtime.after_process_action_decision" and not failed_once:
            failed_once = True
            raise RuntimeError("cross-store crash")

    try:
        async with AgentRuntime(store, provider, processes=bridge, fault=fault) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            pending = await runtime.run_turn(thread.thread_id, "执行任务", request_id="process-2")
            approval = _approval(pending)
            decision = ApprovalDecision(
                outcome=ApprovalOutcome.APPROVED,
                actor="reviewer-a",
                reason="批准",
            )
            with pytest.raises(RuntimeError, match="cross-store crash"):
                await runtime.reply_approval(
                    thread.thread_id,
                    pending.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=decision,
                )
            unchanged = await store.get_thread(thread.thread_id)
            assert _approval(unchanged.turns[-1]).decision is None
            assert (await service.get(approval.plan.action_id)).status is ActionStatus.READY

            with pytest.raises(KernelError) as conflict:
                await runtime.reply_approval(
                    thread.thread_id,
                    pending.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.REJECTED,
                        actor="reviewer-b",
                    ),
                )
            assert conflict.value.code == "approval_conflict"

            repaired = await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=decision,
            )
            assert repaired.status is TurnStatus.WAITING_ACTION
            assert _approval(repaired).decision is not None
    finally:
        await service.close()


async def test_resume_mirrors_action_decision_without_deciding_again(tmp_path: Path) -> None:
    provider = ScriptedProvider([_process_step("print('not-run')"), answer()])
    service, bridge = await _service_and_bridge(tmp_path)
    store = SQLiteSessionStore(tmp_path / "session.db")
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            pending = await runtime.run_turn(thread.thread_id, "执行任务", request_id="process-3")
            approval = _approval(pending)
            action = await service.decide_approval(
                approval.plan.action_id,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="external-reviewer"),
            )
            assert action.status is ActionStatus.READY

            synchronized = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert synchronized.status is TurnStatus.WAITING_ACTION
            projected = _approval(synchronized)
            assert projected.decision == action.approval
            events = await service.events(approval.plan.action_id)
            assert [event.event_type for event in events].count("approval_granted") == 1
    finally:
        await service.close()


async def test_rejected_process_never_enters_worker_queue(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    provider = ScriptedProvider(
        [
            _process_step(
                "open(__import__('sys').argv[1], 'w').write('bad')",
                str(marker),
            ),
            answer("已按拒绝结果处理"),
        ]
    )
    service, bridge = await _service_and_bridge(tmp_path)
    store = SQLiteSessionStore(tmp_path / "session.db")
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            pending = await runtime.run_turn(thread.thread_id, "执行任务", request_id="process-4")
            approval = _approval(pending)
            waiting = await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.REJECTED,
                    actor="reviewer-a",
                    reason="不允许执行",
                ),
            )
            assert waiting.status is TurnStatus.WAITING_ACTION
            assert (
                await ActionWorker(
                    service,
                    poll_seconds=0.01,
                    heartbeat_seconds=1,
                    recovery_interval_seconds=1,
                ).run_once()
                is None
            )
            completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert completed.status is TurnStatus.COMPLETED
    finally:
        await service.close()
    assert not marker.exists()
    result = next(
        item.content for item in completed.items if isinstance(item.content, ToolResultContent)
    )
    assert result.outcome == "failed"
    assert result.process is not None and result.process.status is ActionStatus.DENIED
    assert result.error is not None and result.error.code == "approval_rejected"


async def test_expired_pending_is_preserved_and_late_action_decision_is_mirrored(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([_process_step("print('not-run')"), answer()])
    service, bridge = await _service_and_bridge(tmp_path)
    store = SQLiteSessionStore(tmp_path / "session.db")
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            pending = await runtime.run_turn(
                thread.thread_id,
                "执行任务",
                request_id="process-expired",
                budget=Budget(timeout_seconds=1),
            )
            assert pending.status is TurnStatus.WAITING_APPROVAL
            with pytest.raises(KernelError) as error:
                await runtime.cancel(thread.thread_id, pending.turn_id)
            assert error.value.code == "process_action_not_enabled"
        await asyncio.sleep(1.01)
        before = await store.get_thread(thread.thread_id)
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            assert await store.get_thread(thread.thread_id) == before
            resumed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert resumed.status is TurnStatus.WAITING_APPROVAL
            assert await store.get_thread(thread.thread_id) == before
        approval = _approval(pending)
        assert (await service.get(approval.plan.action_id)).status is ActionStatus.PENDING_APPROVAL
        action = await service.decide_approval(
            approval.plan.action_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="external-reviewer"),
        )
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            synchronized = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert synchronized.status is TurnStatus.WAITING_ACTION
            assert _approval(synchronized).decision == action.approval
    finally:
        await service.close()


async def test_unknown_process_effect_interrupts_without_second_model_step(tmp_path: Path) -> None:
    provider = ScriptedProvider([_process_step("import os\nwhile True: os.write(1, b'x' * 8192)")])
    service, bridge = await _service_and_bridge(
        tmp_path,
        limits=ProcessLimits(stdout_bytes=32, stop_output_bytes=16384),
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    try:
        async with AgentRuntime(store, provider, processes=bridge) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            pending = await runtime.run_turn(thread.thread_id, "执行任务", request_id="unknown")
            approval = _approval(pending)
            await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="reviewer-a",
                ),
            )
            action = await ActionWorker(
                service,
                poll_seconds=0.01,
                heartbeat_seconds=1,
                recovery_interval_seconds=1,
            ).run_once()
            assert action is not None and action.status is ActionStatus.UNKNOWN
            interrupted = await runtime.resume_turn(thread.thread_id, pending.turn_id)
            assert interrupted.status is TurnStatus.INTERRUPTED
    finally:
        await service.close()
    assert len(provider.requests) == 1
    result = next(
        item.content for item in interrupted.items if isinstance(item.content, ToolResultContent)
    )
    assert result.outcome == "unknown"
    assert result.process is not None and result.process.status is ActionStatus.UNKNOWN
    assert interrupted.error is not None and interrupted.error.code == "uncertain_effect"


async def test_process_bridge_rejects_inline_action_execution(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(tmp_path, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=True,
    )
    with pytest.raises(KernelError) as error:
        ProcessAgentBridge(
            service,
            Principal(
                tenant_id="tenant-a",
                subject_id="agent-a",
                framework="harnessix-agent",
            ),
        )
    assert error.value.code == "process_auto_execute_forbidden"
