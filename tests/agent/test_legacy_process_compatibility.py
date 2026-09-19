from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from harnessix.agent.approvals import request_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    ProcessApprovalRequestContent,
    TextContent,
    ThreadCreated,
    ToolCallContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.reducer import get_turn
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
)
from harnessix.models.scripted import FakeProvider
from harnessix.processes.bridge_contracts import (
    PROCESS_ACTION_NAMESPACE,
    AgentProcessCallPlan,
    process_action_identity,
    process_call_request_id,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import digest


async def _historical_turn(
    path: Path, *, waiting_action: bool
) -> tuple[SQLiteSessionStore, UUID, UUID, ProcessApprovalRequestContent]:
    store = SQLiteSessionStore(path / "session.db")
    await store.initialize()
    thread_id, turn_id, call_id = new_id(), new_id(), new_id()
    binding = "b" * 64
    tool_version = f"host-process-action/v1.{binding}"
    arguments = {
        "program": "python",
        "arguments": ["-I", "-c", "print('archived')"],
        "timeout_seconds": 5.0,
    }
    call = ToolCallContent(
        call_id=call_id,
        provider_call_id="historical-process-call",
        tool="host.process",
        tool_version=tool_version,
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        arguments=arguments,
        requires_approval=True,
        tool_fingerprint="f" * 64,
    )
    user = TextContent(kind="user_message", text="历史进程调用")
    user_item, call_item = new_id(), new_id()
    thread = await store.append(
        thread_id,
        [
            EventDraft(payload=ThreadCreated(workspace=str(path))),
            EventDraft(
                turn_id=turn_id,
                payload=TurnStarted(
                    request_id="historical-process",
                    request_fingerprint="0" * 64,
                    budget=Budget(timeout_seconds=3600),
                ),
            ),
            EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=user_item, content=user)),
            EventDraft(
                turn_id=turn_id,
                payload=ItemFinished(item_id=user_item, status=ItemStatus.COMPLETED, content=user),
            ),
            EventDraft(
                turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT)
            ),
            EventDraft(turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.CALLING_MODEL)),
            EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=call_item, content=call)),
            EventDraft(
                turn_id=turn_id,
                payload=ItemFinished(item_id=call_item, status=ItemStatus.COMPLETED, content=call),
            ),
            EventDraft(turn_id=turn_id, payload=UsageRecorded(step=1, usage=Usage())),
            EventDraft(
                turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS)
            ),
        ],
        expected_sequence=0,
    )
    turn = get_turn(thread, turn_id)
    call_fingerprint = request_fingerprint(thread, turn, call)
    request_id = process_call_request_id(thread_id, turn_id, call_id, str(path), call_fingerprint)
    action_fingerprint = "a" * 64
    principal_fingerprint = "c" * 64
    identity = process_action_identity(
        request_id,
        action_fingerprint,
        tool_version,
        binding,
        principal_fingerprint,
    )
    plan_data = {
        "version": "agent-host-process/v1",
        "thread_id": str(thread_id),
        "turn_id": str(turn_id),
        "call_id": str(call_id),
        "workspace": str(path),
        "call_fingerprint": call_fingerprint,
        "request_id": request_id,
        "action_id": str(uuid5(PROCESS_ACTION_NAMESPACE, identity)),
        "action_fingerprint": action_fingerprint,
        "action_tool_version": tool_version,
        "binding_fingerprint": binding,
        "principal_fingerprint": principal_fingerprint,
        "idempotency_key": f"agent-process:{identity}",
        "program": "python",
        "arguments_sha256": digest(tuple(arguments["arguments"])),
        "timeout_seconds": 5.0,
    }
    plan = AgentProcessCallPlan.model_validate_json(
        json.dumps({**plan_data, "approval_fingerprint": digest(plan_data)})
    )
    approval = ProcessApprovalRequestContent(
        approval_id=new_id(),
        call_id=call_id,
        plan=plan,
        request_fingerprint=plan.approval_fingerprint,
    )
    approval_item = new_id()
    thread = await store.append(
        thread_id,
        [
            EventDraft(
                turn_id=turn_id,
                payload=ItemStarted(item_id=approval_item, content=approval),
            ),
            EventDraft(
                turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.WAITING_APPROVAL)
            ),
        ],
        expected_sequence=thread.sequence,
    )
    if waiting_action:
        decided = approval.model_copy(
            update={
                "action_status": ActionStatus.READY,
                "decision": ApprovalRecord(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="historical-reviewer",
                    request_fingerprint=action_fingerprint,
                ),
            }
        )
        thread = await store.append(
            thread_id,
            [
                EventDraft(
                    turn_id=turn_id,
                    occurred_at=decided.decision.decided_at,
                    payload=ItemFinished(
                        item_id=approval_item,
                        status=ItemStatus.COMPLETED,
                        content=decided,
                    ),
                ),
                EventDraft(
                    turn_id=turn_id,
                    payload=TurnStateChanged(status=TurnStatus.WAITING_ACTION),
                ),
            ],
            expected_sequence=thread.sequence,
        )
    return store, thread_id, turn_id, approval


@pytest.mark.parametrize("waiting_action", [False, True])
async def test_historical_process_state_is_readable_but_never_mutated_or_replayed(
    tmp_path: Path, waiting_action: bool
) -> None:
    store, thread_id, turn_id, approval = await _historical_turn(
        tmp_path, waiting_action=waiting_action
    )
    before_thread = await store.get_thread(thread_id)
    before_events = await store.events(thread_id)

    async with AgentRuntime(store, FakeProvider()) as runtime:
        assert await store.get_thread(thread_id) == before_thread
        if not waiting_action:
            with pytest.raises(KernelError) as decision_error:
                await runtime.reply_approval(
                    thread_id,
                    turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED,
                        actor="new-reviewer",
                    ),
                )
            assert decision_error.value.code == "legacy_process_state_archived"
        with pytest.raises(KernelError) as resume_error:
            await runtime.resume_turn(thread_id, turn_id)
        assert resume_error.value.code == "legacy_process_state_archived"

    assert await store.get_thread(thread_id) == before_thread
    assert await store.events(thread_id) == before_events
