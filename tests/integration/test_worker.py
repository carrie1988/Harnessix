from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, Field

from harnessix.bootstrap import build_service
from harnessix.domain.errors import ActionConflictError
from harnessix.domain.models import (
    ActionSnapshot,
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    ExecutionOutcome,
    ReconciliationOutcome,
    RiskLevel,
    utc_now,
)
from harnessix.domain.registry import ToolDefinition
from harnessix.runtime import ActionService
from harnessix.settings import Settings
from harnessix.worker import ActionWorker, WorkerLeaseLostError
from tests.helpers import action_request


class SlowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delay_seconds: float = Field(gt=0)


class SlowExecutor:
    async def execute(self, action: ActionSnapshot, arguments: BaseModel) -> ExecutionOutcome:
        parsed = SlowInput.model_validate(arguments)
        await asyncio.sleep(parsed.delay_seconds)
        return ExecutionOutcome.succeeded(output={"action_id": str(action.request.action_id)})

    async def reconcile(self, action: ActionSnapshot) -> ReconciliationOutcome:
        return ReconciliationOutcome.unknown(
            code="test_reconciliation_unsupported",
            message=f"测试工具不对账：{action.request.action_id}",
        )


class UnknownExecutor:
    async def execute(self, action: ActionSnapshot, arguments: BaseModel) -> ExecutionOutcome:
        return ExecutionOutcome.unknown(code="test_effect_unknown", message="测试效果未知")

    async def reconcile(self, action: ActionSnapshot) -> ReconciliationOutcome:
        return ReconciliationOutcome.unknown(
            code="test_reconciliation_unsupported",
            message=f"测试工具不对账：{action.request.action_id}",
        )


@pytest_asyncio.fixture
async def queued_service(tmp_path: Path) -> AsyncIterator[ActionService]:
    service = build_service(
        Settings(
            database_path=tmp_path / "harnessix.db",
            demo_database_path=tmp_path / "demo-external.db",
            execution_mode="queued",
            lease_seconds=1,
            worker_heartbeat_seconds=0.1,
            worker_poll_seconds=0.01,
            recovery_interval_seconds=0.05,
        ),
        worker_id="test-worker",
    )
    await service.initialize()
    try:
        yield service
    finally:
        await service.close()


def _worker(service: ActionService) -> ActionWorker:
    return ActionWorker(
        service,
        poll_seconds=0.01,
        heartbeat_seconds=0.1,
        recovery_interval_seconds=0.05,
    )


async def test_queued_action_is_executed_by_worker(queued_service: ActionService) -> None:
    request = action_request("system.echo", {"message": "queued"})

    ready = await queued_service.submit(request)
    completed = await _worker(queued_service).run_once()

    assert ready.status is ActionStatus.READY
    assert completed is not None
    assert completed.status is ActionStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result.output["message"] == "queued"


async def test_approval_only_enqueues_action(queued_service: ActionService) -> None:
    request = action_request(
        "demo.issue.create",
        {"title": "队列审批"},
        idempotency_key="worker:approval",
    )

    pending = await queued_service.submit(request)
    ready = await queued_service.decide_approval(
        request.action_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer-a"),
    )
    completed = await _worker(queued_service).run_once()

    assert pending.status is ActionStatus.PENDING_APPROVAL
    assert ready.status is ActionStatus.READY
    assert completed is not None
    assert completed.status is ActionStatus.SUCCEEDED


async def test_ready_action_can_only_be_claimed_once(queued_service: ActionService) -> None:
    request = action_request("system.echo", {"message": "claim-once"})
    await queued_service.submit(request)

    first, second = await asyncio.gather(
        queued_service.journal.claim_next_ready(
            worker_id="worker-a", lease_expires_at=utc_now() + timedelta(seconds=10)
        ),
        queued_service.journal.claim_next_ready(
            worker_id="worker-b", lease_expires_at=utc_now() + timedelta(seconds=10)
        ),
    )

    claimed = [snapshot for snapshot in (first, second) if snapshot is not None]
    assert len(claimed) == 1
    assert claimed[0].request.action_id == request.action_id


async def test_stale_worker_cannot_advance_state(queued_service: ActionService) -> None:
    request = action_request("system.echo", {"message": "lease-owner"})
    await queued_service.submit(request)
    claimed = await queued_service.journal.claim_next_ready(
        worker_id="worker-a", lease_expires_at=utc_now() + timedelta(seconds=10)
    )
    assert claimed is not None

    with pytest.raises(ActionConflictError):
        await queued_service.journal.transition(
            request.action_id,
            expected={ActionStatus.LEASED},
            target=ActionStatus.RUNNING,
            event_type="stale_worker_started",
            required_lease_owner="worker-b",
        )


async def test_expired_unstarted_lease_returns_to_ready(queued_service: ActionService) -> None:
    request = action_request("system.echo", {"message": "expired-lease"})
    await queued_service.submit(request)
    claimed = await queued_service.journal.claim_next_ready(
        worker_id="expired-worker",
        lease_expires_at=utc_now() - timedelta(seconds=1),
    )
    assert claimed is not None

    with pytest.raises(ActionConflictError):
        await queued_service.journal.transition(
            request.action_id,
            expected={ActionStatus.LEASED},
            target=ActionStatus.RUNNING,
            event_type="expired_worker_started",
            required_lease_owner="expired-worker",
        )

    recovered = await queued_service.journal.recover_expired()
    snapshot = await queued_service.get(request.action_id)

    assert recovered == [request.action_id]
    assert snapshot.status is ActionStatus.READY


async def test_heartbeat_renews_lease_during_action(queued_service: ActionService) -> None:
    queued_service.lease_seconds = 5
    queued_service.registry.register(
        ToolDefinition(
            name="test.slow",
            version="1.0.0",
            description="验证 Worker 心跳续租",
            input_model=SlowInput,
            effect_class=EffectClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            executor=SlowExecutor(),
        )
    )
    request = action_request("test.slow", {"delay_seconds": 1.2})
    await queued_service.submit(request)

    completed = await _worker(queued_service).run_once()
    events = await queued_service.events(request.action_id)

    assert completed is not None
    assert completed.status is ActionStatus.SUCCEEDED
    assert completed.version > 6
    assert "lease_renewed" not in {event.event_type for event in events}


@pytest.mark.parametrize("unknown_outcome", [False, True])
async def test_execution_commit_wins_renewal_race(
    queued_service: ActionService,
    monkeypatch: pytest.MonkeyPatch,
    unknown_outcome: bool,
) -> None:
    queued_service.lease_seconds = 5
    if unknown_outcome:
        queued_service.registry.register(
            ToolDefinition(
                name="test.unknown",
                version="1.0.0",
                description="验证UNKNOWN执行提交",
                input_model=SlowInput,
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                executor=UnknownExecutor(),
            )
        )
    request = (
        action_request("test.unknown", {"delay_seconds": 0.1})
        if unknown_outcome
        else action_request("system.echo", {"message": "terminal-commit"})
    )
    await queued_service.submit(request)
    transition = queued_service.journal.transition
    terminal_committed = asyncio.Event()
    release_transition = asyncio.Event()

    async def delay_terminal_return(*args, **kwargs):
        completed = await transition(*args, **kwargs)
        if kwargs.get("event_type") == "execution_completed":
            terminal_committed.set()
            await release_transition.wait()
        return completed

    monkeypatch.setattr(queued_service.journal, "transition", delay_terminal_return)
    worker = ActionWorker(
        queued_service,
        poll_seconds=0.01,
        heartbeat_seconds=0.05,
        recovery_interval_seconds=0.05,
    )

    execution = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(terminal_committed.wait(), timeout=5)
    completed = await asyncio.wait_for(execution, timeout=5)
    release_transition.set()

    assert completed is not None
    assert completed.status is (ActionStatus.UNKNOWN if unknown_outcome else ActionStatus.SUCCEEDED)
    assert completed.result is not None
    if unknown_outcome:
        assert completed.result.error is not None
        assert completed.result.error.code == "test_effect_unknown"
    else:
        assert completed.result.output["message"] == "terminal-commit"


async def test_failed_renewal_while_running_still_reports_lost_lease(
    queued_service: ActionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_service.lease_seconds = 5
    queued_service.registry.register(
        ToolDefinition(
            name="test.renewal-loss",
            version="1.0.0",
            description="验证真实续租失败仍中止执行",
            input_model=SlowInput,
            effect_class=EffectClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            executor=SlowExecutor(),
        )
    )
    request = action_request("test.renewal-loss", {"delay_seconds": 0.5})
    await queued_service.submit(request)

    async def reject_renewal(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(queued_service.journal, "renew_lease", reject_renewal)
    get_action = queued_service.get
    recovered_before_read = False

    async def recover_before_read(action_id):
        nonlocal recovered_before_read
        if not recovered_before_read:
            recovered_before_read = True
            assert await queued_service.journal.recover_expired(
                utc_now() + timedelta(seconds=6)
            ) == [request.action_id]
        return await get_action(action_id)

    monkeypatch.setattr(queued_service, "get", recover_before_read)
    worker = ActionWorker(
        queued_service,
        poll_seconds=0.01,
        heartbeat_seconds=0.05,
        recovery_interval_seconds=0.05,
    )

    with pytest.raises(WorkerLeaseLostError):
        await worker.run_once()

    recovered = await get_action(request.action_id)
    assert recovered.status is ActionStatus.UNKNOWN
    assert recovered.result is not None
    assert recovered.result.error is not None
    assert recovered.result.error.code == "lease_expired"


async def test_operational_stats_report_queue_state(queued_service: ActionService) -> None:
    ready_request = action_request("system.echo", {"message": "stats"})
    pending_request = action_request(
        "demo.issue.create",
        {"title": "等待审批"},
        idempotency_key="stats:approval",
    )
    await queued_service.submit(ready_request)
    await queued_service.submit(pending_request)

    before = await queued_service.journal.operational_stats()
    await _worker(queued_service).run_once()
    after = await queued_service.journal.operational_stats()

    assert before.ready_count == 1
    assert before.pending_approval_count == 1
    assert before.oldest_ready_at is not None
    assert after.ready_count == 0
    assert after.pending_approval_count == 1


async def test_metrics_collection_failure_does_not_change_execution_result(
    queued_service: ActionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = action_request("system.echo", {"message": "best-effort-metrics"})
    await queued_service.submit(request)

    async def unavailable_stats() -> None:
        raise RuntimeError("metrics query unavailable")

    monkeypatch.setattr(queued_service.journal, "operational_stats", unavailable_stats)
    completed = await _worker(queued_service).run_once()

    assert completed is not None
    assert completed.status is ActionStatus.SUCCEEDED
