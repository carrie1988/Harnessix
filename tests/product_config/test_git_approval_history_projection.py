"""完整合法历史的纯语义投影；声明夹具不证明 Session/Router MAC 或真实资源来源。"""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import FrozenInstanceError, fields
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.lifecycle import prepare_fork_snapshot
from harnessix.agent.models import (
    AgentEvent,
    Budget,
    ErrorContent,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ThreadArchived,
    ThreadCreated,
    ThreadForked,
    ToolResultContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.reducer import apply_event, get_turn, replay
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, canonical_digest
from harnessix.product_config.git_approval_history_projection import (
    OriginalGitApprovalHistory,
    interpret_git_approval_history,
)
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.trusted_actions.agent_gateway_output import build_approval, build_result
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    action_audit_event_digest,
    build_audit_event,
)
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from tests.product_config.git_delivery_plan_support import NOW, observe_state
from tests.support.git_delivery_observed_core import make_observed_case


class Transcript:
    """通过原 Reducer 构造合法事件，不直接修改 Thread phase 来模拟成功。"""

    def __init__(self, case, *, call=None):
        self.case = case
        self.events = []
        self.thread = None
        self.clock = NOW
        self.request = None
        self.finish = None
        self.cancel_sources = ()
        self.checkpoint = None
        self.route_events = (
            build_audit_event(
                case.plan.route,
                sequence=1,
                from_state=None,
                to_state="pending_approval",
                previous_digest=None,
                occurred_at=NOW,
            ),
        )
        self.route = self.route_snapshot()
        self.add(ThreadCreated(workspace=str(case.workspace_root)), thread_event=True)
        self.add(
            TurnStarted(
                request_id="projection-declaration",
                request_fingerprint="0" * 64,
                budget=Budget(),
            )
        )
        self.item(TextContent(kind="user_message", text="正式交付请求"))
        self.add(TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT))
        self.add(TurnStateChanged(status=TurnStatus.CALLING_MODEL))
        call = call or case.core.call
        self.item(call)
        self.add(UsageRecorded(step=1, usage=Usage()))
        self.add(TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS))
        turn = get_turn(self.thread, case.core.turn_id)
        request = build_approval(
            self.thread,
            turn,
            call,
            self.route,
            TrustedActionReview(diff_artifact=case.plan.review_artifact),
            presentation="patch_batch",
        )
        self.prepared = ProductGitPreparedLink(plan=case.plan, approval=request)
        self.request = self.add(ItemStarted(item_id=uuid4(), content=request))
        self.add(TurnStateChanged(status=TurnStatus.WAITING_APPROVAL))

    def add(self, payload, *, at=None, thread_event=False):
        self.clock = at or self.clock + timedelta(microseconds=1)
        event = AgentEvent(
            thread_id=self.case.core.thread_id,
            turn_id=None if thread_event else self.case.core.turn_id,
            sequence=len(self.events) + 1,
            occurred_at=self.clock,
            payload=payload,
        )
        self.thread = apply_event(self.thread, event)
        self.events.append(event)
        return event

    def item(self, content):
        started = self.add(ItemStarted(item_id=uuid4(), content=content))
        return self.add(
            ItemFinished(
                item_id=started.payload.item_id, status=ItemStatus.COMPLETED, content=content
            )
        )

    def route_snapshot(self):
        last = self.route_events[-1]
        return ActionRouteSnapshotV2(
            plan=self.case.plan.route,
            state=last.to_state,
            sequence=last.sequence,
            last_event_digest=last.digest,
            updated_at=last.occurred_at,
        )

    def router_decision(self, outcome, *, actor="reviewer", reason="核验完成", at=None):
        execution = self.case.plan.route.execution
        self.checkpoint = ExecutionApprovalCheckpoint(
            plan_id=execution.plan_id,
            plan_fingerprint=execution.fingerprint,
            decision=ApprovalRecord(
                outcome=outcome,
                actor=actor,
                reason=reason,
                request_fingerprint=execution.fingerprint,
                decided_at=at or NOW + timedelta(seconds=10),
            ),
        )
        previous = self.route_events[0]
        self.route_events = (
            previous,
            build_audit_event(
                self.case.plan.route,
                sequence=2,
                from_state="pending_approval",
                to_state="ready" if outcome is ApprovalOutcome.APPROVED else "denied",
                previous_digest=previous.digest,
                approval_outcome=outcome,
                approval_actor=actor,
                error_code="approval_rejected" if outcome is ApprovalOutcome.REJECTED else None,
                occurred_at=self.checkpoint.decision.decided_at,
            ),
        )
        self.route = self.route_snapshot()

    def decide(self, outcome, *, executing=False, **decision):
        self.router_decision(outcome, **decision)
        decided = self.prepared.approval.model_copy(
            update={
                "route_state": self.route.state,
                "decision": self.checkpoint.decision.model_copy(
                    update={"request_fingerprint": self.prepared.approval.request_fingerprint}
                ),
            }
        )
        self.finish = self.add(
            ItemFinished(
                item_id=self.request.payload.item_id,
                status=ItemStatus.COMPLETED,
                content=decided,
            ),
            at=decided.decision.decided_at,
        )
        if executing:
            self.add(TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS))
        return self

    def rejection_result(self, *, origin="execution"):
        return build_result(
            self.route,
            self.case.core.call,
            ActionExecutionOutcome(kind="failed", error_code="approval_rejected"),
            origin=origin,
            approval=self.finish.payload.content,
        )

    def cancel(self):
        cancelling = self.add(
            TurnStateChanged(status=TurnStatus.CANCELLING), at=NOW + timedelta(seconds=20)
        )
        # 原 Router 在取消请求之后决定，早于 Session _finish 的批量结算事件。
        self.router_decision(
            ApprovalOutcome.REJECTED,
            actor="system.cancel",
            reason="turn_cancelled",
            at=cancelling.occurred_at + timedelta(microseconds=1),
        )
        error = AgentFailure(code="cancelled", message="取消已生效")
        self.finish = self.add(
            ItemFinished(
                item_id=self.request.payload.item_id,
                status=ItemStatus.CANCELLED,
                content=self.prepared.approval,
                error=error,
            ),
            at=cancelling.occurred_at + timedelta(microseconds=2),
        )
        result = self.item(
            ToolResultContent(
                call_id=self.case.core.call.call_id,
                outcome="cancelled",
                error=AgentFailure(code="cancelled", message="用户取消了尚未批准的Action"),
            )
        )
        self.item(ErrorContent(failure=error))
        terminal = self.add(TurnStateChanged(status=TurnStatus.CANCELLED, error=error))
        self.cancel_sources = (cancelling, self.finish, result, terminal)
        return self

    def interpret(self, *, check=lambda: None, **updates):
        values = dict(
            history=AuthenticatedThreadHistory(self.thread, tuple(self.events)),
            prepared=self.prepared,
            route=self.route,
            route_events=self.route_events,
            approval=self.checkpoint,
        )
        values.update(updates)
        return interpret_git_approval_history(**values, checkpoint=check)


@pytest.fixture
def transcript(tmp_path):
    # CAS 仅用于复用完整 Plan2 声明；不连接 Provider，不创建认证 Session/Router。
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        case, _ = make_observed_case(GitMaterialCAS(store), tmp_path)
        yield Transcript(case)


def reject(transcript, **updates):
    with pytest.raises(KernelError) as caught:
        transcript.interpret(**updates)
    assert caught.value.code == "git_approval_history_changed"
    assert caught.value.message == "Git审批历史无法核验"
    assert caught.value.__cause__ is None
    assert "private-sensitive" not in str(caught.value)
    return caught.value


def test_pending_full_replay_and_pure_output(transcript):
    before = copy.deepcopy((transcript.thread, transcript.prepared, transcript.route_events))
    result = transcript.interpret()
    assert result.state == "pending"
    assert result.request_event is transcript.request
    assert result.decision_event is result.router_approval is result.route_decision is None
    assert result.cancel_events == ()
    assert before == (transcript.thread, transcript.prepared, transcript.route_events)
    assert replay(transcript.events) == transcript.thread
    assert repr(result) == "OriginalGitApprovalHistory(state='pending')"
    assert not hasattr(result, "can_execute")


def test_interface_is_private_frozen_slots_without_expected_outcome(transcript):
    result = transcript.interpret()
    assert type(result) is OriginalGitApprovalHistory
    assert {field.name for field in fields(result)} == {
        "state",
        "request_event",
        "decision_event",
        "cancel_events",
        "router_approval",
        "route_decision",
    }
    assert all(not field.repr for field in fields(result) if field.name != "state")
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.state = "approved"
    signature = inspect.signature(interpret_git_approval_history)
    assert tuple(signature.parameters) == (
        "history",
        "prepared",
        "route",
        "route_events",
        "approval",
        "checkpoint",
    )
    assert signature.parameters["checkpoint"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("executing", [False, True])
@pytest.mark.parametrize("outcome", [ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED])
def test_original_human_decision_crosses_fingerprint_domains(transcript, executing, outcome):
    transcript.decide(outcome, executing=executing)
    result = transcript.interpret()
    assert result.state == ("approved" if outcome is ApprovalOutcome.APPROVED else "denied")
    assert result.request_event is transcript.request
    assert result.decision_event is transcript.finish
    assert result.router_approval is transcript.checkpoint
    assert result.route_decision is transcript.route_events[1]
    assert result.cancel_events == ()
    assert (
        transcript.finish.payload.content.decision.request_fingerprint
        != transcript.checkpoint.decision.request_fingerprint
    )


@pytest.mark.parametrize("origin", ["execution", "recovery"])
def test_denied_accepts_original_rejection_settlement_and_complete_suffix(transcript, origin):
    transcript.decide(ApprovalOutcome.REJECTED, executing=True)
    transcript.item(transcript.rejection_result(origin=origin))
    if origin == "execution":
        transcript.add(TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT))
        transcript.add(TurnStateChanged(status=TurnStatus.CALLING_MODEL))
        transcript.add(UsageRecorded(step=2, usage=Usage()))
        transcript.add(TurnStateChanged(status=TurnStatus.FINALIZING))
        transcript.add(TurnStateChanged(status=TurnStatus.COMPLETED))
    else:
        # 原 Reducer 禁止恢复元数据把中断 Turn 冒充成功，使用正式 INTERRUPTED 结算。
        error = AgentFailure(code="process_interrupted", message="执行已中断")
        transcript.item(ErrorContent(failure=error))
        transcript.add(TurnStateChanged(status=TurnStatus.INTERRUPTED, error=error))
    transcript.add(ThreadArchived(reason="已结算"), thread_event=True)
    assert transcript.interpret().state == "denied"
    assert replay(transcript.events) == transcript.thread


def test_cancelled_is_four_original_events_not_a_fabricated_human_decision(transcript):
    transcript.cancel()
    result = transcript.interpret()
    assert result.state == "cancelled"
    assert result.decision_event is None
    assert result.cancel_events == transcript.cancel_sources
    assert all(a is b for a, b in zip(result.cancel_events, transcript.cancel_sources, strict=True))
    assert result.request_event is transcript.request
    assert result.router_approval is transcript.checkpoint
    assert result.route_decision is transcript.route_events[1]
    assert transcript.finish.payload.content.decision is None
    assert transcript.checkpoint.decision.decided_at != transcript.finish.occurred_at
    assert transcript.checkpoint.decision.decided_at != result.cancel_events[0].occurred_at


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "cancelled"])
def test_checkpoint_covers_complete_events_including_suffix(transcript, state, monkeypatch):
    if state == "cancelled":
        transcript.cancel()
    elif state != "pending":
        transcript.decide(
            ApprovalOutcome.APPROVED if state == "approved" else ApprovalOutcome.REJECTED
        )
    from harnessix.product_config import git_approval_history_projection as module

    seen = []
    checkpoints = []
    original = module.apply_event

    def observe(thread, event):
        assert len(checkpoints) > len(seen)
        seen.append(event)
        return original(thread, event)

    monkeypatch.setattr(module, "apply_event", observe)
    assert transcript.interpret(check=lambda: checkpoints.append(None)).state == state
    assert seen == transcript.events
    assert len(checkpoints) >= len(transcript.events) + len(transcript.route_events) + 1


@pytest.mark.parametrize(
    "error",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        TimeoutError("private-sensitive"),
        KernelError("owner_closed", "原控制异常"),
        OSError("private-sensitive"),
    ],
)
@pytest.mark.parametrize("at", [1, 7, 14])
def test_checkpoint_control_exception_preserves_original_instance(transcript, error, at):
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == at:
            raise error

    with pytest.raises(type(error)) as caught:
        transcript.interpret(check=check)
    assert caught.value is error


def changed_audit(event, **updates):
    """反例重算原摘要并验证声明；有效摘要不是来源认证或正确审批语义。"""
    candidate = event.model_copy(update=updates)
    candidate = candidate.model_copy(update={"digest": action_audit_event_digest(candidate)})
    return type(event).model_validate_json(candidate.model_dump_json(), strict=True)


def history_with(transcript, events, *, valid=True):
    """合法替代序列先经原 replay；明确损坏的反例不伪造合法验收。"""
    events = tuple(events)
    return AuthenticatedThreadHistory(replay(events) if valid else transcript.thread, events)


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
def test_full_plan_variants_remain_declarations_without_writes(tmp_path, fmt, action):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        case, _ = make_observed_case(GitMaterialCAS(store), tmp_path, fmt=fmt, action=action)
        transcript = Transcript(case).decide(ApprovalOutcome.APPROVED)
        before = observe_state(case)
        assert transcript.interpret().state == "approved"
        assert observe_state(case) == before


@pytest.mark.parametrize("field", ["provider_call_id", "arguments", "tool_version"])
def test_complete_original_call_not_only_call_id_is_required(transcript, field):
    values = {
        "provider_call_id": "private-sensitive",
        "arguments": {"patches": []},
        "tool_version": "another-version",
    }
    # 原 Session 序列和 prepared 声明都合法，只有 Core.call 与原请求上下文不同。
    other = Transcript(
        transcript.case, call=transcript.case.core.call.model_copy(update={field: values[field]})
    )
    assert replay(other.events) == other.thread
    reject(other)


@pytest.mark.parametrize("field", ["request_fingerprint", "plan_id", "call_id", "diff_artifact"])
def test_prepared_request_cannot_replace_original_started_content(transcript, field):
    values = {
        "request_fingerprint": "e" * 64,
        "plan_id": UUID(int=999),
        "call_id": UUID(int=999),
        "diff_artifact": transcript.prepared.plan.review_artifact.model_copy(
            update={"sha256": "e" * 64}
        ),
    }
    prepared = transcript.prepared.model_copy(
        update={"approval": transcript.prepared.approval.model_copy(update={field: values[field]})}
    )
    reject(transcript, prepared=prepared)


@pytest.mark.parametrize(
    "kind",
    ["empty", "prefix_only", "sequence_gap", "duplicate_id", "foreign_thread", "duplicate_request"],
)
def test_complete_root_replay_rejects_damaged_history(transcript, kind):
    events = list(transcript.events)
    if kind == "empty":
        events = []
    elif kind == "prefix_only":
        events = events[4:]
    elif kind == "sequence_gap":
        events[5] = events[5].model_copy(update={"sequence": events[5].sequence + 1})
    elif kind == "duplicate_id":
        events[5] = events[5].model_copy(update={"event_id": events[4].event_id})
    elif kind == "foreign_thread":
        events[5] = events[5].model_copy(update={"thread_id": UUID(int=999)})
    else:
        events.append(
            transcript.request.model_copy(update={"sequence": len(events) + 1, "event_id": uuid4()})
        )
    reject(transcript, history=history_with(transcript, events, valid=False))


@pytest.mark.parametrize(
    "field", ["sequence", "updated_at", "active_turn_id", "turn_budget", "turn_phase"]
)
def test_full_replayed_projection_must_equal_supplied_thread(transcript, field):
    thread = transcript.thread
    if field in {"turn_budget", "turn_phase"}:
        turn = thread.turns[0]
        updates = (
            {"budget": Budget(timeout_seconds=999)}
            if field == "turn_budget"
            else {"status": TurnStatus.EXECUTING_TOOLS}
        )
        thread = thread.model_copy(update={"turns": (turn.model_copy(update=updates),)})
    else:
        values = {"sequence": thread.sequence - 1, "updated_at": NOW, "active_turn_id": None}
        thread = thread.model_copy(update={field: values[field]})
    reject(transcript, history=AuthenticatedThreadHistory(thread, tuple(transcript.events)))


@pytest.mark.parametrize("seconds", [-1, 0, 10, 11])
def test_original_decision_time_and_turn_cutoff_are_not_refreshable(transcript, seconds):
    transcript.decide(ApprovalOutcome.APPROVED)
    events = list(transcript.events)
    if seconds in {-1, 0}:
        decision = transcript.finish.payload.content.decision.model_copy(
            update={"decided_at": NOW + timedelta(seconds=seconds)}
        )
        content = transcript.finish.payload.content.model_copy(update={"decision": decision})
        payload = transcript.finish.payload.model_copy(update={"content": content})
        events[-1] = transcript.finish.model_copy(update={"payload": payload})
    else:
        # 原预算设为决定时刻或更早，不能拿最终 Thread 的延长预算救回决定。
        started = events[1]
        original_created = events[1].occurred_at
        exact_cutoff = (transcript.finish.occurred_at - original_created).total_seconds()
        budget = Budget(timeout_seconds=exact_cutoff if seconds == 10 else 9)
        events[1] = started.model_copy(
            update={"payload": started.payload.model_copy(update={"budget": budget})}
        )
    error = reject(transcript, history=history_with(transcript, events, valid=False))
    assert error.__suppress_context__


@pytest.mark.parametrize(
    "field", ["outcome", "actor", "reason", "decided_at", "request_fingerprint"]
)
def test_session_completed_decision_must_match_router_in_its_own_domain(transcript, field):
    transcript.decide(ApprovalOutcome.APPROVED)
    content = transcript.finish.payload.content
    values = {
        "outcome": ApprovalOutcome.REJECTED,
        "actor": "private-sensitive",
        "reason": "private-sensitive",
        "decided_at": content.decision.decided_at + timedelta(microseconds=1),
        "request_fingerprint": transcript.checkpoint.decision.request_fingerprint,
    }
    decision = content.decision.model_copy(update={field: values[field]})
    updated = content.model_copy(
        update={"decision": decision, **({"route_state": "denied"} if field == "outcome" else {})}
    )
    event = transcript.finish.model_copy(
        update={
            "payload": transcript.finish.payload.model_copy(update={"content": updated}),
            "occurred_at": decision.decided_at,
        }
    )
    events = (*transcript.events[:-1], event)
    valid = field != "request_fingerprint"
    reject(transcript, history=history_with(transcript, events, valid=valid))


@pytest.mark.parametrize(
    "field",
    [
        "plan_id",
        "plan_fingerprint",
        "request_fingerprint",
        "actor",
        "reason",
        "decided_at",
        "outcome",
    ],
)
def test_execution_checkpoint_binding_and_decision_drift_is_rejected(transcript, field):
    transcript.decide(ApprovalOutcome.APPROVED)
    approval = transcript.checkpoint
    if field in {"plan_id", "plan_fingerprint"}:
        value = (
            UUID(int=999) if field == "plan_id" else transcript.prepared.approval.plan_fingerprint
        )
        approval = approval.model_copy(update={field: value})
    else:
        values = {
            "request_fingerprint": transcript.prepared.approval.request_fingerprint,
            "actor": "private-sensitive",
            "reason": "private-sensitive",
            "decided_at": approval.decision.decided_at + timedelta(seconds=1),
            "outcome": ApprovalOutcome.REJECTED,
        }
        approval = approval.model_copy(
            update={"decision": approval.decision.model_copy(update={field: values[field]})}
        )
    reject(transcript, approval=approval)


@pytest.mark.parametrize(
    "field",
    [
        "plan_id",
        "plan_fingerprint",
        "resource_sha256",
        "policy_id",
        "policy_version",
        "approval_actor_sha256",
        "occurred_at",
    ],
)
@pytest.mark.parametrize("index", [0, 1])
def test_route_full_chain_identity_policy_resources_actor_time(transcript, field, index):
    transcript.decide(ApprovalOutcome.APPROVED)
    if index == 0 and field == "approval_actor_sha256":
        updates = {
            "approval_actor_sha256": canonical_digest("private-sensitive"),
            "approval_outcome": ApprovalOutcome.APPROVED,
        }
    else:
        values = {
            "plan_id": UUID(int=999),
            "plan_fingerprint": "e" * 64,
            "resource_sha256": "e" * 64,
            "policy_id": "private-sensitive",
            "policy_version": "another-version",
            "approval_actor_sha256": canonical_digest("private-sensitive"),
            "occurred_at": NOW + timedelta(seconds=30),
        }
        updates = {field: values[field]}
    events = list(transcript.route_events)
    events[index] = changed_audit(events[index], **updates)
    if index == 0:
        events[1] = changed_audit(events[1], previous_digest=events[0].digest)
    last = events[-1]
    route = transcript.route.model_copy(
        update={"last_event_digest": last.digest, "updated_at": last.occurred_at}
    )
    reject(transcript, route_events=tuple(events), route=route)


@pytest.mark.parametrize("field", ["state", "sequence", "last_event_digest", "updated_at", "plan"])
def test_current_route_cannot_be_a_stale_or_different_snapshot(transcript, field):
    transcript.decide(ApprovalOutcome.APPROVED)
    values = {
        "state": "pending_approval",
        "sequence": 1,
        "last_event_digest": "e" * 64,
        "updated_at": NOW,
        "plan": transcript.route.plan.model_copy(update={"fingerprint": "e" * 64}),
    }
    reject(transcript, route=transcript.route.model_copy(update={field: values[field]}))


@pytest.mark.parametrize(
    "field",
    ["executor_id", "output_sha256", "artifact_sha256", "external_action_id", "reconciliation"],
)
def test_approval_route_event_cannot_smuggle_execution_metadata(transcript, field):
    transcript.decide(ApprovalOutcome.APPROVED)
    values = {
        "executor_id": "product.git_checkpoint",
        "output_sha256": "e" * 64,
        "artifact_sha256": "e" * 64,
        "external_action_id": UUID(int=999),
        "reconciliation": "failed",
    }
    decision = changed_audit(transcript.route_events[1], **{field: values[field]})
    route = transcript.route.model_copy(update={"last_event_digest": decision.digest})
    reject(transcript, route=route, route_events=(transcript.route_events[0], decision))


@pytest.mark.parametrize(
    "state", ["running", "reconciling", "unknown", "succeeded", "failed", "manual_intervention"]
)
def test_route_execute_or_reconcile_suffix_is_outside_narrow_projection(transcript, state):
    transcript.decide(ApprovalOutcome.APPROVED)
    previous = transcript.route_events[-1]
    third = build_audit_event(
        transcript.route.plan,
        sequence=3,
        from_state="ready",
        to_state=state,
        previous_digest=previous.digest,
        occurred_at=NOW + timedelta(seconds=20),
    )
    events = (*transcript.route_events, third)
    route = transcript.route.model_copy(
        update={
            "state": state,
            "sequence": 3,
            "last_event_digest": third.digest,
            "updated_at": third.occurred_at,
        }
    )
    reject(transcript, route=route, route_events=events)


@pytest.mark.parametrize("state", ["ready", "denied"])
def test_initial_allow_or_policy_denial_is_not_an_approval(transcript, state):
    initial = build_audit_event(
        transcript.route.plan,
        sequence=1,
        from_state=None,
        to_state=state,
        previous_digest=None,
        occurred_at=NOW,
    )
    route = transcript.route.model_copy(
        update={"state": state, "last_event_digest": initial.digest}
    )
    reject(transcript, route=route, route_events=(initial,))


@pytest.mark.parametrize("missing", ["checkpoint", "route_decision", "session_decision"])
def test_no_partial_three_party_decision_is_promoted(transcript, missing):
    if missing == "session_decision":
        transcript.router_decision(ApprovalOutcome.APPROVED)
        reject(transcript)
    else:
        transcript.decide(ApprovalOutcome.APPROVED)
        if missing == "checkpoint":
            reject(transcript, approval=None)
        else:
            first = transcript.route_events[0]
            route = transcript.route.model_copy(
                update={
                    "state": "pending_approval",
                    "sequence": 1,
                    "last_event_digest": first.digest,
                    "updated_at": first.occurred_at,
                }
            )
            reject(transcript, route=route, route_events=(first,))


def test_pending_requires_persisted_original_waiting_boundary(transcript):
    events = tuple(transcript.events[:-1])
    reject(transcript, history=history_with(transcript, events))


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "unknown"])
def test_approved_rejects_any_result_even_before_route_has_advanced(transcript, outcome):
    transcript.decide(ApprovalOutcome.APPROVED, executing=True)
    action = ActionExecutionOutcome(
        kind=outcome, error_code=None if outcome == "succeeded" else "action_failed"
    )
    result = build_result(
        transcript.route,
        transcript.case.core.call,
        action,
        origin="execution",
        approval=transcript.finish.payload.content,
    )
    transcript.item(result)
    assert replay(transcript.events) == transcript.thread
    reject(transcript)


@pytest.mark.parametrize("settled", [False, True])
def test_full_approved_after_cancel_suffix_is_never_usable_approved(transcript, settled):
    transcript.decide(ApprovalOutcome.APPROVED)
    transcript.add(TurnStateChanged(status=TurnStatus.CANCELLING))
    if settled:
        error = AgentFailure(code="uncertain_effect", message="存在未知效果")
        transcript.item(
            ToolResultContent(
                call_id=transcript.case.core.call.call_id, outcome="unknown", error=error
            )
        )
        transcript.item(ErrorContent(failure=error))
        transcript.add(TurnStateChanged(status=TurnStatus.INTERRUPTED, error=error))
        transcript.add(ThreadArchived(reason="等待恢复"), thread_event=True)
    assert replay(transcript.events) == transcript.thread
    reject(transcript)


@pytest.mark.parametrize("kind", ["unknown", "output", "artifact", "wrong_plan", "wrong_reason"])
def test_denied_settlement_cannot_smuggle_unknown_or_new_effects(transcript, kind):
    transcript.decide(ApprovalOutcome.REJECTED, executing=True)
    result = transcript.rejection_result()
    if kind == "unknown":
        result = result.model_copy(
            update={
                "outcome": "unknown",
                "trusted_action": result.trusted_action.model_copy(update={"state": "unknown"}),
            }
        )
    elif kind == "output":
        result = result.model_copy(update={"output": "private-sensitive"})
    elif kind == "artifact":
        result = result.model_copy(
            update={
                "trusted_action": result.trusted_action.model_copy(
                    update={"artifact_sha256": "e" * 64}
                )
            }
        )
    elif kind == "wrong_plan":
        # 正式 Reducer 同样拒绝跨计划；以损坏反例交给完整重放而不是模拟合法结算。
        result = result.model_copy(
            update={
                "action_id": UUID(int=999),
                "trusted_action": result.trusted_action.model_copy(
                    update={"plan_id": UUID(int=999)}
                ),
            }
        )
        event = AgentEvent(
            thread_id=transcript.thread.thread_id,
            turn_id=transcript.case.core.turn_id,
            sequence=len(transcript.events) + 1,
            payload=ItemStarted(item_id=uuid4(), content=result),
        )
        reject(
            transcript, history=history_with(transcript, (*transcript.events, event), valid=False)
        )
        return
    else:
        result = result.model_copy(
            update={"error": AgentFailure(code="action_failed", message="private-sensitive")}
        )
    transcript.item(result)
    reject(transcript)


@pytest.mark.parametrize("last", ["request", "cancelling", "approval_cancelled", "result", "error"])
def test_partial_cancel_never_becomes_cancelled_or_pending(transcript, last):
    transcript.cancel()
    sources = transcript.cancel_sources
    positions = {
        "request": transcript.request.sequence + 1,
        "cancelling": sources[0].sequence,
        "approval_cancelled": sources[1].sequence,
        "result": sources[2].sequence,
        "error": sources[3].sequence - 1,
    }
    events = transcript.events[: positions[last]]
    reject(transcript, history=history_with(transcript, events))


@pytest.mark.parametrize(
    "field", ["actor", "reason", "outcome", "time_before_cancel", "time_after_settlement"]
)
def test_cancel_requires_original_system_rejection_and_natural_time_window(transcript, field):
    transcript.cancel()
    decision = transcript.checkpoint.decision
    values = {
        "actor": "system.other",
        "reason": "another_reason",
        "outcome": ApprovalOutcome.APPROVED,
        "time_before_cancel": NOW,
        "time_after_settlement": NOW + timedelta(seconds=30),
    }
    if field.startswith("time_"):
        field_name = "decided_at"
    else:
        field_name = field
    decision = decision.model_copy(update={field_name: values[field]})
    approval = transcript.checkpoint.model_copy(update={"decision": decision})
    # 同步 Route 声明让反例针对取消语义，而不是依靠跨库时间/actor 不匹配。
    event = changed_audit(
        transcript.route_events[1],
        approval_outcome=decision.outcome,
        approval_actor_sha256=canonical_digest(decision.actor),
        occurred_at=decision.decided_at,
    )
    route = transcript.route.model_copy(
        update={"last_event_digest": event.digest, "updated_at": event.occurred_at}
    )
    reject(
        transcript, approval=approval, route=route, route_events=(transcript.route_events[0], event)
    )


def test_system_cancel_cannot_be_wrapped_as_completed_human_denial(transcript):
    transcript.decide(ApprovalOutcome.REJECTED, actor="system.cancel", reason="turn_cancelled")
    assert replay(transcript.events) == transcript.thread
    reject(transcript)


@pytest.mark.parametrize("field", ["output", "action_id", "diff_artifact", "error"])
def test_cancelled_result_has_no_effect_or_artifact_claim(transcript, field):
    transcript.cancel()
    result_event = transcript.cancel_sources[2]
    values = {
        "output": "private-sensitive",
        "action_id": UUID(int=999),
        "diff_artifact": transcript.case.plan.review_artifact,
        "error": AgentFailure(code="action_failed", message="private-sensitive"),
    }
    result = result_event.payload.content.model_copy(update={field: values[field]})
    events = list(transcript.events)
    for index in (result_event.sequence - 2, result_event.sequence - 1):
        events[index] = events[index].model_copy(
            update={"payload": events[index].payload.model_copy(update={"content": result})}
        )
    reject(transcript, history=history_with(transcript, events, valid=field != "diff_artifact"))


def test_fork_inherited_history_never_becomes_child_original_request(transcript):
    transcript.decide(ApprovalOutcome.REJECTED, executing=True)
    transcript.item(transcript.rejection_result())
    transcript.add(TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT))
    transcript.add(TurnStateChanged(status=TurnStatus.CALLING_MODEL))
    transcript.add(UsageRecorded(step=2, usage=Usage()))
    transcript.add(TurnStateChanged(status=TurnStatus.FINALIZING))
    transcript.add(TurnStateChanged(status=TurnStatus.COMPLETED))
    snapshot = prepare_fork_snapshot(
        transcript.thread,
        request_id="fork-declaration",
        through_turn_id=None,
        policy=ToolResultViewPolicy(),
    ).snapshot
    child_id = UUID(int=999)
    event = AgentEvent(
        thread_id=child_id,
        sequence=1,
        occurred_at=NOW + timedelta(seconds=30),
        payload=ThreadForked(workspace=transcript.thread.workspace, snapshot=snapshot),
    )
    child = replay((event,))
    assert any(item.content == transcript.case.core.call for item in snapshot.items)
    assert child.turns == ()
    # 即使伪造 Core 的子 Thread 归属声明，继承模型历史里也没有本 Thread 原请求。
    core = transcript.prepared.plan.core.model_copy(update={"thread_id": child_id})
    plan = transcript.prepared.plan.model_copy(update={"core": core})
    prepared = transcript.prepared.model_copy(update={"plan": plan})
    reject(transcript, prepared=prepared, history=AuthenticatedThreadHistory(child, (event,)))


@pytest.mark.parametrize("kind", ["route_list", "history_list", "approval_dict", "history_none"])
def test_declared_container_types_are_not_coerced(transcript, kind):
    if kind == "route_list":
        reject(transcript, route_events=list(transcript.route_events))
    elif kind == "history_list":
        reject(transcript, history=AuthenticatedThreadHistory(transcript.thread, transcript.events))
    elif kind == "approval_dict":
        transcript.decide(ApprovalOutcome.APPROVED)
        reject(transcript, approval=transcript.checkpoint.model_dump())
    else:
        reject(transcript, history=None)


def test_duplicate_completed_decision_and_invalid_suffix_are_not_truncated(transcript):
    transcript.decide(ApprovalOutcome.APPROVED)
    duplicate = transcript.finish.model_copy(
        update={"sequence": len(transcript.events) + 1, "event_id": uuid4()}
    )
    reject(
        transcript, history=history_with(transcript, (*transcript.events, duplicate), valid=False)
    )


def test_control_exception_at_final_checkpoint_is_not_wrapped(transcript):
    transcript.cancel()
    checkpoints = []
    transcript.interpret(check=lambda: checkpoints.append(None))
    error = KernelError("owner_closed", "原控制异常")
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == len(checkpoints):
            raise error

    with pytest.raises(KernelError) as caught:
        transcript.interpret(check=check)
    assert caught.value is error


def test_cancelled_result_must_start_after_original_approval_item_cancelled(transcript):
    transcript.cancel()
    events = list(transcript.events)
    cancelling, _, result, _ = transcript.cancel_sources
    started = events.pop(result.sequence - 2)
    started = started.model_copy(
        update={"occurred_at": cancelling.occurred_at - timedelta(seconds=1)}
    )
    events.insert(cancelling.sequence - 1, started)
    events = [event.model_copy(update={"sequence": index}) for index, event in enumerate(events, 1)]
    # 原 Reducer 可结算这条声明，但它不符合原 _finish 的审批前取消源事件顺序。
    reject(transcript, history=history_with(transcript, events))


def test_cancel_before_waiting_boundary_is_not_original_waiting_approval_cancel(transcript):
    transcript.cancel()
    waiting = next(
        event
        for event in transcript.events
        if isinstance(event.payload, TurnStateChanged)
        and event.payload.status is TurnStatus.WAITING_APPROVAL
    )
    events = [event for event in transcript.events if event is not waiting]
    events = [event.model_copy(update={"sequence": index}) for index, event in enumerate(events, 1)]
    reject(transcript, history=history_with(transcript, events))
