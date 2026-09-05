from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    AgentEvent,
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    TextContent,
    Thread,
    ThreadCreated,
    ToolCallContent,
    ToolResultContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.reducer import get_turn, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ActionFailure,
    ActionResult,
    ActionSnapshot,
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    Principal,
    ToolDescriptor,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.models._history import messages_for
from harnessix.models.contracts import ModelRequest
from harnessix.models.scripted import FakeProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_bridge import prepare_process_action
from harnessix.processes.bridge_contracts import AgentProcessCallPlan
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.processes.session_projection import (
    process_action_state,
    process_approval_decision,
    process_approval_request,
)
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal


@dataclass(frozen=True)
class ProcessCase:
    store: SQLiteSessionStore
    thread_id: UUID
    turn_id: UUID
    call: ToolCallContent
    scope: ToolExecutionScope
    definition: ToolDescriptor
    principal: Principal
    plan: AgentProcessCallPlan
    pending: ActionSnapshot
    approved: ActionSnapshot
    request: ProcessApprovalRequestContent
    decision: ProcessApprovalRequestContent
    approval_item_id: UUID


async def _append(store: SQLiteSessionStore, thread_id: UUID, drafts: list[EventDraft]) -> Thread:
    current = await store.get_thread(thread_id)
    return await store.append(
        thread_id,
        drafts,
        expected_sequence=current.sequence,
    )


async def _process_case(tmp_path: Path) -> ProcessCase:
    store = SQLiteSessionStore(tmp_path / "session.db")
    await store.initialize()
    thread_id, turn_id = new_id(), new_id()
    await store.append(
        thread_id,
        [EventDraft(payload=ThreadCreated(workspace=str(tmp_path)))],
        expected_sequence=0,
    )
    tool = process_action_tool(lambda: HostProcessRuntime(tmp_path, {"python": sys.executable}))
    definition = tool.descriptor()
    call = ToolCallContent(
        call_id=new_id(),
        provider_call_id="process-call",
        tool=definition.name,
        tool_version=definition.version,
        effect_class=definition.effect_class,
        arguments={
            "program": "python",
            "arguments": ["-I", "-c", "print('not-executed')"],
            "timeout_seconds": 5.0,
        },
        requires_approval=True,
        tool_fingerprint=tool_fingerprint(definition),
    )
    user = TextContent(kind="user_message", text="只构造进程Action，不执行")
    user_item, call_item = new_id(), new_id()
    thread = await _append(
        store,
        thread_id,
        [
            EventDraft(
                turn_id=turn_id,
                payload=TurnStarted(
                    request_id="process-projection",
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
                turn_id=turn_id,
                payload=TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
            ),
            EventDraft(
                turn_id=turn_id,
                payload=TurnStateChanged(status=TurnStatus.CALLING_MODEL),
            ),
            EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=call_item, content=call)),
            EventDraft(
                turn_id=turn_id,
                payload=ItemFinished(item_id=call_item, status=ItemStatus.COMPLETED, content=call),
            ),
            EventDraft(
                turn_id=turn_id,
                payload=UsageRecorded(step=1, usage=Usage()),
            ),
            EventDraft(
                turn_id=turn_id,
                payload=TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
            ),
        ],
    )
    scope = ToolExecutionScope.for_pending_call(thread, turn_id, call)
    principal = Principal(
        tenant_id="tenant-a",
        subject_id="agent-a",
        framework="harnessix-agent",
        roles=("developer",),
    )
    prepared = prepare_process_action(call, scope, definition, principal)
    registry = ToolRegistry()
    registry.register(tool)
    service = ActionService(
        journal=SQLiteEffectJournal(tmp_path / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await service.initialize()
    try:
        pending = await service.submit(prepared.request)
        assert pending.status is ActionStatus.PENDING_APPROVAL
        request = process_approval_request(
            call,
            scope,
            definition,
            principal,
            prepared.plan,
            pending,
            approval_id=new_id(),
        )
        approval_item_id = new_id()
        await _append(
            store,
            thread_id,
            [
                EventDraft(
                    turn_id=turn_id,
                    payload=ItemStarted(item_id=approval_item_id, content=request),
                ),
                EventDraft(
                    turn_id=turn_id,
                    payload=TurnStateChanged(status=TurnStatus.WAITING_APPROVAL),
                ),
            ],
        )
        approved = await service.decide_approval(
            prepared.request.action_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
        )
        decision = process_approval_decision(request, call, scope, definition, principal, approved)
    finally:
        await service.close()
    assert decision.decision is not None
    return ProcessCase(
        store=store,
        thread_id=thread_id,
        turn_id=turn_id,
        call=call,
        scope=scope,
        definition=definition,
        principal=principal,
        plan=prepared.plan,
        pending=pending,
        approved=approved,
        request=request,
        decision=decision,
        approval_item_id=approval_item_id,
    )


async def _enter_waiting_action(case: ProcessCase) -> Thread:
    assert case.decision.decision is not None
    return await _append(
        case.store,
        case.thread_id,
        [
            EventDraft(
                turn_id=case.turn_id,
                occurred_at=case.decision.decision.decided_at,
                payload=ItemFinished(
                    item_id=case.approval_item_id,
                    status=ItemStatus.COMPLETED,
                    content=case.decision,
                ),
            ),
            EventDraft(
                turn_id=case.turn_id,
                payload=TurnStateChanged(status=TurnStatus.WAITING_ACTION),
            ),
        ],
    )


async def _record_state(
    case: ProcessCase,
    snapshot: ActionSnapshot,
    *,
    origin: Literal["execution", "recovery"] = "execution",
) -> ProcessActionStateContent:
    content = process_action_state(
        case.decision,
        case.call,
        case.scope,
        case.definition,
        case.principal,
        snapshot,
        origin=origin,
    )
    item_id = new_id()
    await _append(
        case.store,
        case.thread_id,
        [
            EventDraft(
                turn_id=case.turn_id,
                payload=ItemStarted(item_id=item_id, content=content),
            ),
            EventDraft(
                turn_id=case.turn_id,
                payload=ItemFinished(
                    item_id=item_id,
                    status=ItemStatus.COMPLETED,
                    content=content,
                ),
            ),
        ],
    )
    return content


async def test_process_action_waiting_and_result_projection_is_replayable(tmp_path: Path) -> None:
    case = await _process_case(tmp_path)
    assert case.decision.decision is not None
    current = await case.store.get_thread(case.thread_id)

    # Process审批不能绕过持久Action等待边界；整批失败也不能留下半条决定。
    with pytest.raises(KernelError):
        await case.store.append(
            case.thread_id,
            [
                EventDraft(
                    turn_id=case.turn_id,
                    occurred_at=case.decision.decision.decided_at,
                    payload=ItemFinished(
                        item_id=case.approval_item_id,
                        status=ItemStatus.COMPLETED,
                        content=case.decision,
                    ),
                ),
                EventDraft(
                    turn_id=case.turn_id,
                    payload=TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
                ),
            ],
            expected_sequence=current.sequence,
        )
    assert await case.store.get_thread(case.thread_id) == current

    waiting = await _enter_waiting_action(case)
    assert get_turn(waiting, case.turn_id).status is TurnStatus.WAITING_ACTION
    running = case.approved.model_copy(update={"status": ActionStatus.RUNNING})
    await _record_state(case, running)

    # 活跃Action观察不能提前恢复Agent工具循环，也不能伪造Tool Result。
    current = await case.store.get_thread(case.thread_id)
    for payload in (
        TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
        ItemStarted(
            item_id=new_id(),
            content=ToolResultContent(call_id=case.call.call_id, outcome="failed"),
        ),
    ):
        with pytest.raises(KernelError):
            await case.store.append(
                case.thread_id,
                [EventDraft(turn_id=case.turn_id, payload=payload)],
                expected_sequence=current.sequence,
            )

    failed = case.approved.model_copy(
        update={
            "status": ActionStatus.FAILED,
            "result": ActionResult(
                status=ActionStatus.FAILED,
                error=ActionFailure(code="fixture_failure", message="未执行测试夹具"),
            ),
        }
    )
    terminal = await _record_state(case, failed)
    await _append(
        case.store,
        case.thread_id,
        [
            EventDraft(
                turn_id=case.turn_id,
                payload=TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
            )
        ],
    )

    forged = terminal.effect.model_copy(update={"result_fingerprint": "f" * 64})
    current = await case.store.get_thread(case.thread_id)
    with pytest.raises(KernelError):
        await case.store.append(
            case.thread_id,
            [
                EventDraft(
                    turn_id=case.turn_id,
                    payload=ItemStarted(
                        item_id=new_id(),
                        content=ToolResultContent(
                            call_id=case.call.call_id,
                            outcome="failed",
                            action_id=case.plan.action_id,
                            process=forged,
                        ),
                    ),
                )
            ],
            expected_sequence=current.sequence,
        )

    result = ToolResultContent(
        call_id=case.call.call_id,
        outcome="failed",
        error=AgentFailure(code="fixture_failure", message="未执行测试夹具"),
        action_id=case.plan.action_id,
        process=terminal.effect,
    )
    result_item = new_id()
    final = await _append(
        case.store,
        case.thread_id,
        [
            EventDraft(
                turn_id=case.turn_id,
                payload=ItemStarted(item_id=result_item, content=result),
            ),
            EventDraft(
                turn_id=case.turn_id,
                payload=ItemFinished(
                    item_id=result_item,
                    status=ItemStatus.COMPLETED,
                    content=result,
                ),
            ),
        ],
    )
    assert replay(await case.store.events(case.thread_id)) == final

    turn = get_turn(final, case.turn_id)
    history = tuple(
        item
        for item in turn.items
        if item.status is ItemStatus.COMPLETED
        and isinstance(item.content, TextContent | ToolCallContent | ToolResultContent)
    )
    messages = messages_for(
        ModelRequest(
            thread_id=case.thread_id,
            turn_id=case.turn_id,
            step=2,
            history=history,
            tools=(),
            budget=turn.budget,
            remaining_tokens=100,
        )
    )
    tool_message = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
    assert set(tool_message) == {"outcome", "output", "error"}
    assert "process" not in json.dumps(messages)


async def test_process_projection_rejects_client_decision_and_unverified_snapshot(
    tmp_path: Path,
) -> None:
    case = await _process_case(tmp_path)
    with pytest.raises(ValidationError):
        ProcessApprovalRequestContent(
            **case.request.model_dump(exclude={"decision", "action_status"}),
            action_status=ActionStatus.READY,
            decision=ApprovalRecord(
                outcome=ApprovalOutcome.APPROVED,
                actor="forged-client",
                request_fingerprint=case.request.request_fingerprint,
            ),
        )
    altered = case.pending.model_copy(
        update={"request": case.pending.request.model_copy(update={"action_id": new_id()})}
    )
    with pytest.raises(KernelError) as error:
        process_approval_request(
            case.call,
            case.scope,
            case.definition,
            case.principal,
            case.plan,
            altered,
            approval_id=new_id(),
        )
    assert error.value.code == "process_projection_mismatch"
    assert case.decision.decision is not None
    with pytest.raises(ValidationError):
        ProcessApprovalRequestContent.model_validate_json(
            case.decision.model_copy(
                update={"action_status": ActionStatus.DENIED}
            ).model_dump_json()
        )


async def test_agent_v9_boundary_and_restart_preserve_waiting_action(tmp_path: Path) -> None:
    case = await _process_case(tmp_path)
    waiting_approval = await case.store.get_thread(case.thread_id)
    async with AgentRuntime(case.store, FakeProvider()) as runtime:
        with pytest.raises(KernelError) as error:
            await runtime.reply_approval(
                case.thread_id,
                case.turn_id,
                case.request.approval_id,
                fingerprint=case.request.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="session-client"),
            )
        assert error.value.code == "process_action_not_enabled"
    assert await case.store.get_thread(case.thread_id) == waiting_approval

    waiting = await _enter_waiting_action(case)
    turn = get_turn(waiting, case.turn_id)
    for payload in (
        TurnStateChanged(status=TurnStatus.WAITING_ACTION),
        ItemStarted(item_id=new_id(), content=case.request),
        ItemStarted(
            item_id=new_id(),
            content=process_action_state(
                case.decision,
                case.call,
                case.scope,
                case.definition,
                case.principal,
                case.approved.model_copy(update={"status": ActionStatus.RUNNING}),
                origin="execution",
            ),
        ),
    ):
        with pytest.raises(ValidationError):
            EventDraft(schema_version=8, turn_id=case.turn_id, payload=payload)
    assert (
        EventDraft(payload=TurnStateChanged(status=TurnStatus.WAITING_ACTION)).schema_version == 9
    )

    before = await case.store.get_thread(case.thread_id)
    async with AgentRuntime(case.store, FakeProvider()):
        assert await case.store.get_thread(case.thread_id) == before
    after = await case.store.get_thread(case.thread_id)
    assert get_turn(after, case.turn_id).status is TurnStatus.WAITING_ACTION
    assert turn == get_turn(after, case.turn_id)
    with pytest.raises(KernelError) as error:
        async with AgentRuntime(case.store, FakeProvider()) as runtime:
            await runtime.resume_turn(case.thread_id, case.turn_id)
    assert error.value.code == "turn_not_resumable"
    with pytest.raises(KernelError) as error:
        async with AgentRuntime(case.store, FakeProvider()) as runtime:
            await runtime.cancel(case.thread_id, case.turn_id)
    assert error.value.code == "process_action_not_enabled"
    assert await case.store.get_thread(case.thread_id) == before


def test_v8_event_schema_remains_frozen() -> None:
    root = Path(__file__).parents[2] / "spec"
    assert json.loads((root / "agent-event-v8.schema.json").read_text())["title"] == "AgentEvent"
    assert AgentEvent.model_json_schema() != json.loads(
        (root / "agent-event-v8.schema.json").read_text()
    )
