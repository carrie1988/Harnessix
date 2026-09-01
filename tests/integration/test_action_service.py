from __future__ import annotations

from datetime import timedelta

import pytest

from harnessix.domain.errors import (
    ActionConflictError,
    IdempotencyConflictError,
    IllegalTransitionError,
)
from harnessix.domain.models import (
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    utc_now,
)
from harnessix.runtime import ActionService, action_fingerprint
from tests.helpers import action_request


async def test_echo_runs_without_approval_and_records_lifecycle(
    service: ActionService,
) -> None:
    request = action_request("system.echo", {"message": "你好 Harnessix"})

    snapshot = await service.submit(request)
    events = await service.events(request.action_id)

    assert snapshot.status is ActionStatus.SUCCEEDED
    assert snapshot.result is not None
    assert snapshot.result.output["message"] == "你好 Harnessix"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == "action_received"
    assert events[-1].to_status is ActionStatus.SUCCEEDED


async def test_issue_requires_approval_and_is_idempotent(service: ActionService) -> None:
    request = action_request(
        "demo.issue.create",
        {"title": "生产事故", "body": "验证幂等执行"},
        idempotency_key="issue:production-incident",
        effect_hint=EffectClass.IDEMPOTENT_WRITE,
    )

    pending = await service.submit(request)
    succeeded = await service.decide_approval(
        request.action_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
    )
    duplicate = await service.submit(
        action_request(
            "demo.issue.create",
            {"title": "生产事故", "body": "验证幂等执行"},
            idempotency_key="issue:production-incident",
            effect_hint=EffectClass.IDEMPOTENT_WRITE,
        )
    )

    assert pending.status is ActionStatus.PENDING_APPROVAL
    assert succeeded.status is ActionStatus.SUCCEEDED
    assert succeeded.result is not None
    assert succeeded.result.receipt is not None
    assert duplicate.request.action_id == request.action_id
    assert duplicate.result == succeeded.result


async def test_idempotency_key_rejects_different_payload(service: ActionService) -> None:
    first = action_request("demo.issue.create", {"title": "A"}, idempotency_key="issue:conflict")
    second = action_request("demo.issue.create", {"title": "B"}, idempotency_key="issue:conflict")
    await service.submit(first)

    with pytest.raises(IdempotencyConflictError):
        await service.submit(second)


async def test_action_id_rejects_mutated_request(service: ActionService) -> None:
    first = action_request("system.echo", {"message": "A"})
    await service.submit(first)
    mutated = first.model_copy(update={"arguments": {"message": "B"}})

    with pytest.raises(ActionConflictError):
        await service.submit(mutated)


async def test_rejected_approval_never_executes_effect(service: ActionService) -> None:
    request = action_request(
        "demo.issue.create", {"title": "禁止创建"}, idempotency_key="issue:rejected"
    )
    await service.submit(request)

    denied = await service.decide_approval(
        request.action_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.REJECTED,
            actor="reviewer-a",
            reason="风险不可接受",
        ),
    )

    assert denied.status is ActionStatus.DENIED
    assert denied.result is not None
    assert denied.result.error is not None
    assert denied.result.error.code == "approval_rejected"


async def test_uncertain_effect_is_reconciled_without_reexecution(
    service: ActionService,
) -> None:
    request = action_request(
        "demo.issue.create",
        {"title": "网络中断", "simulate_uncertain_after_commit": True},
        idempotency_key="issue:uncertain",
    )
    await service.submit(request)

    unknown = await service.decide_approval(
        request.action_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
    )
    reconciled = await service.reconcile(request.action_id)

    assert unknown.status is ActionStatus.UNKNOWN
    assert unknown.result is not None
    assert unknown.result.error is not None
    assert unknown.result.error.retriable is False
    assert reconciled.status is ActionStatus.SUCCEEDED
    assert reconciled.result is not None
    assert reconciled.result.receipt is not None
    assert reconciled.result.receipt.idempotency_key == "issue:uncertain"


async def test_raw_secret_is_rejected_before_policy(service: ActionService) -> None:
    request = action_request("system.echo", {"message": "x", "api_key": "raw-secret"})

    snapshot = await service.submit(request)

    assert snapshot.status is ActionStatus.FAILED
    assert snapshot.result is not None
    assert snapshot.result.error is not None
    assert snapshot.result.error.code == "raw_secret_rejected"


async def test_effect_hint_mismatch_is_rejected(service: ActionService) -> None:
    request = action_request("system.echo", {"message": "x"}, effect_hint=EffectClass.DESTRUCTIVE)

    snapshot = await service.submit(request)

    assert snapshot.status is ActionStatus.FAILED
    assert snapshot.result is not None
    assert snapshot.result.error is not None
    assert snapshot.result.error.code == "effect_mismatch"


async def test_expired_running_lease_becomes_unknown(service: ActionService) -> None:
    request = action_request(
        "demo.issue.create", {"title": "租约恢复"}, idempotency_key="issue:lease"
    )
    tool = service.registry.get(request.tool)
    await service.journal.create_action(request, tool.descriptor(), action_fingerprint(request))
    await service.journal.transition(
        request.action_id,
        expected={ActionStatus.RECEIVED},
        target=ActionStatus.VALIDATED,
        event_type="test_validated",
    )
    await service.journal.transition(
        request.action_id,
        expected={ActionStatus.VALIDATED},
        target=ActionStatus.POLICY_EVALUATED,
        event_type="test_policy_evaluated",
    )
    await service.journal.transition(
        request.action_id,
        expected={ActionStatus.POLICY_EVALUATED},
        target=ActionStatus.READY,
        event_type="test_ready",
    )
    await service.journal.transition(
        request.action_id,
        expected={ActionStatus.READY},
        target=ActionStatus.LEASED,
        event_type="test_leased",
        lease_owner="dead-worker",
        lease_expires_at=utc_now() - timedelta(seconds=1),
    )
    await service.journal.transition(
        request.action_id,
        expected={ActionStatus.LEASED},
        target=ActionStatus.RUNNING,
        event_type="test_running",
    )

    recovered = await service.journal.recover_expired()
    snapshot = await service.get(request.action_id)

    assert recovered == [request.action_id]
    assert snapshot.status is ActionStatus.UNKNOWN


async def test_journal_rejects_illegal_state_transition(service: ActionService) -> None:
    request = action_request("system.echo", {"message": "非法状态"})
    tool = service.registry.get(request.tool)
    await service.journal.create_action(request, tool.descriptor(), action_fingerprint(request))

    with pytest.raises(IllegalTransitionError):
        await service.journal.transition(
            request.action_id,
            expected={ActionStatus.RECEIVED},
            target=ActionStatus.RUNNING,
            event_type="illegal",
        )
