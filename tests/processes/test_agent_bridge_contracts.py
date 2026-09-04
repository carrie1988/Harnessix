import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.approvals import execution_fingerprint, tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.ids import new_id
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import (
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    Principal,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_bridge import prepare_process_action, process_snapshot_matches
from harnessix.processes.bridge_contracts import AgentProcessCallPlan
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.storage import SQLiteEffectJournal


def _factory(root, *, environment=None):
    return lambda: HostProcessRuntime(
        root,
        {"python": sys.executable},
        environment=environment,
    )


def _principal(subject="agent-a"):
    return Principal(
        tenant_id="tenant-a",
        subject_id=subject,
        framework="harnessix-agent",
        roles=("developer",),
    )


def _call_and_scope(root, definition, *, arguments=None, thread_id=None, turn_id=None):
    thread_id = thread_id or new_id()
    turn_id = turn_id or new_id()
    call = ToolCallContent(
        call_id=new_id(),
        provider_call_id="provider-call-1",
        tool=definition.name,
        tool_version=definition.version,
        effect_class=definition.effect_class,
        arguments=arguments
        or {
            "program": "python",
            "arguments": ["-I", "-c", "print('ok')"],
            "timeout_seconds": 5.0,
        },
        requires_approval=True,
        tool_fingerprint=tool_fingerprint(definition),
    )
    fingerprint = execution_fingerprint(thread_id, turn_id, str(root), call)
    return call, ToolExecutionScope(
        thread_id=thread_id,
        turn_id=turn_id,
        call_id=call.call_id,
        workspace=str(root),
        request_fingerprint=fingerprint,
    )


def test_prepare_is_deterministic_and_binds_complete_action_identity(tmp_path):
    definition = process_action_tool(_factory(tmp_path)).descriptor()
    call, scope = _call_and_scope(tmp_path, definition)

    first = prepare_process_action(call, scope, definition, _principal())
    second = prepare_process_action(call, scope, definition, _principal())

    assert first == second
    assert first.request.action_id == first.plan.action_id
    assert first.request.idempotency_key == first.plan.idempotency_key
    assert first.request.context.session_id == str(scope.thread_id)
    assert first.request.context.run_id == str(scope.turn_id)
    assert first.request.metadata["harnessix.agent_process"]["request_id"] == first.plan.request_id
    assert first.plan.action_tool_version == definition.version
    assert first.plan.arguments_sha256 != call.tool_fingerprint
    assert len(first.plan.approval_fingerprint) == 64


def test_action_identity_changes_with_authority_or_intent(tmp_path):
    definition = process_action_tool(_factory(tmp_path)).descriptor()
    call, scope = _call_and_scope(tmp_path, definition)
    original = prepare_process_action(call, scope, definition, _principal())

    other_subject = prepare_process_action(call, scope, definition, _principal("agent-b"))
    other_call, other_scope = _call_and_scope(
        tmp_path,
        definition,
        arguments={"program": "python", "arguments": ["-V"], "timeout_seconds": 5.0},
        thread_id=scope.thread_id,
        turn_id=scope.turn_id,
    )
    other_intent = prepare_process_action(other_call, other_scope, definition, _principal())
    other_binding = process_action_tool(_factory(tmp_path, environment={})).descriptor()
    rebound_call, rebound_scope = _call_and_scope(
        tmp_path,
        other_binding,
        thread_id=scope.thread_id,
        turn_id=scope.turn_id,
    )
    rebound = prepare_process_action(rebound_call, rebound_scope, other_binding, _principal())

    assert (
        len(
            {
                original.plan.action_id,
                other_subject.plan.action_id,
                other_intent.plan.action_id,
                rebound.plan.action_id,
            }
        )
        == 4
    )


def test_contract_rejects_forged_plan_and_tool_call(tmp_path):
    definition = process_action_tool(_factory(tmp_path)).descriptor()
    call, scope = _call_and_scope(tmp_path, definition)
    prepared = prepare_process_action(call, scope, definition, _principal())

    forged = prepared.plan.model_copy(update={"workspace": "/tmp/other"})
    with pytest.raises(ValidationError):
        AgentProcessCallPlan.model_validate_json(forged.model_dump_json())

    changed_call = call.model_copy(update={"requires_approval": False})
    changed_scope = scope.__class__(
        thread_id=scope.thread_id,
        turn_id=scope.turn_id,
        call_id=scope.call_id,
        workspace=scope.workspace,
        request_fingerprint=execution_fingerprint(
            scope.thread_id, scope.turn_id, scope.workspace, changed_call
        ),
    )
    with pytest.raises(KernelError) as error:
        prepare_process_action(changed_call, changed_scope, definition, _principal())
    assert error.value.code == "tool_contract_changed"


async def test_journal_snapshot_is_the_only_matching_action_fact(tmp_path):
    tool = process_action_tool(_factory(tmp_path))
    definition = tool.descriptor()
    call, scope = _call_and_scope(tmp_path, definition)
    prepared = prepare_process_action(call, scope, definition, _principal())
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
        snapshot = await service.submit(prepared.request)
        duplicate = await service.submit(prepared.request)
        assert snapshot.status is ActionStatus.PENDING_APPROVAL
        assert duplicate.request.action_id == snapshot.request.action_id
        assert process_snapshot_matches(
            call, scope, definition, _principal(), prepared.plan, snapshot
        )
        approved = await service.decide_approval(
            prepared.request.action_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
        )
        assert approved.status is ActionStatus.READY
        assert process_snapshot_matches(
            call, scope, definition, _principal(), prepared.plan, approved
        )

        altered = snapshot.model_copy(
            update={
                "request": snapshot.request.model_copy(
                    update={"metadata": {"harnessix.agent_process": {"forged": True}}}
                )
            }
        )
        assert not process_snapshot_matches(
            call, scope, definition, _principal(), prepared.plan, altered
        )
        unapproved_ready = snapshot.model_copy(update={"status": ActionStatus.READY})
        assert not process_snapshot_matches(
            call, scope, definition, _principal(), prepared.plan, unapproved_ready
        )
        forged_rejection = snapshot.model_copy(
            update={
                "approval": ApprovalRecord(
                    outcome=ApprovalOutcome.REJECTED,
                    actor="reviewer-a",
                    request_fingerprint=prepared.plan.action_fingerprint,
                )
            }
        )
        assert not process_snapshot_matches(
            call, scope, definition, _principal(), prepared.plan, forged_rejection
        )
    finally:
        await service.close()


def test_generated_agent_process_plan_schema_matches_code():
    path = Path(__file__).parents[2] / "spec" / "agent-process-call-plan-v1.schema.json"
    assert json.loads(path.read_text()) == AgentProcessCallPlan.model_json_schema()
