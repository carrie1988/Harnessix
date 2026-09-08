from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    AgentEvent,
    EventDraft,
    ItemStarted,
    TextContent,
    TurnStateChanged,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer import apply_event
from harnessix.agent.usage import (
    ModelAttempt,
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.context.compaction import replay_compaction_candidate, validate_compaction
from harnessix.context.compaction_ledger_contracts import (
    CompactionAttemptFinished,
    CompactionAttemptStarted,
    CompactionPlanned,
    CompactionRecord,
    CompactionRejected,
    CompactionSummarized,
    CompactionUsageObserved,
)
from tests.context.test_compaction import change_turn, plan, summary, thread_with

FAILURE = AgentFailure(code="provider_stream_incomplete", message="摘要未完成")


def apply(thread, payload, **fields):
    return apply_event(
        thread,
        AgentEvent(
            thread_id=thread.thread_id,
            turn_id=thread.active_turn_id,
            sequence=thread.sequence + 1,
            payload=payload,
            **fields,
        ),
    )


async def planned(thread=None):
    source = thread or thread_with()
    prepared = await plan(source)
    return (
        source,
        prepared,
        apply(
            source,
            CompactionPlanned(plan=prepared.plan, decisions=prepared.model_history.new_decisions),
        ),
    )


def start(thread, **fields):
    record = thread.turns[-1].compactions[-1]
    event = ModelAttemptStarted(
        **{
            "attempt_id": uuid4(),
            "step": record.plan.model_step,
            "index": 1,
            "provider": "offline",
            "requested_model": "summary-model",
            **fields,
        }
    )
    return apply(
        thread, CompactionAttemptStarted(compaction_id=record.plan.compaction_id, event=event)
    )


def observation(thread, *, complete=True, **fields):
    record = thread.turns[-1].compactions[-1]
    event = ModelUsageObserved(
        attempt_id=record.attempt.attempt_id,
        usage=UsageObservation(
            completeness="complete" if complete else "partial",
            input_tokens=100,
            output_tokens=30 if complete else None,
        ),
        actual_model="summary-model",
        response_id="summary-response",
    ).model_copy(update=fields)
    return apply(
        thread, CompactionUsageObserved(compaction_id=record.plan.compaction_id, event=event)
    )


def finish(thread, outcome="completed"):
    record = thread.turns[-1].compactions[-1]
    return apply(
        thread,
        CompactionAttemptFinished(
            compaction_id=record.plan.compaction_id,
            event=ModelAttemptFinished(
                attempt_id=record.attempt.attempt_id,
                outcome=outcome,
                error=None if outcome == "completed" else FAILURE,
            ),
        ),
    )


def reject(thread, outcome="failed"):
    record = thread.turns[-1].compactions[-1]
    return apply(
        thread,
        CompactionRejected(
            compaction_id=record.plan.compaction_id, outcome=outcome, failure=FAILURE
        ),
    )


async def test_summary_ledger_preserves_steps_items_and_cumulative_accounting():
    source, prepared, thread = await planned()
    thread = start(thread)
    thread = observation(thread, complete=False)
    thread = observation(thread, complete=False)
    assert thread.turns[-1].usage == Usage(input_tokens=100)
    assert not thread.turns[-1].usage_is_complete
    thread = finish(observation(thread))
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    assert replay_compaction_candidate(source, prepared.plan, summary(prepared)) == candidate
    thread = apply(
        thread,
        CompactionSummarized(
            compaction_id=prepared.plan.compaction_id,
            summary=summary(prepared),
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ),
    )
    turn = thread.turns[-1]
    record = turn.compactions[-1]
    assert record.status == "summarized"
    assert record.attempt.status == "completed"
    assert turn.usage == Usage(input_tokens=100, output_tokens=30)
    assert turn.model_steps == turn.usage_step == 0
    assert not turn.model_attempts
    assert turn.items == source.turns[-1].items
    assert not turn.usage_is_complete  # 摘要请求不能冒充普通步骤覆盖。
    assert len(turn.accounted_attempts) == 1
    assert CompactionRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    "field,value", [("source_history_sha256", "0" * 64), ("source_event_sequence", 1)]
)
async def test_tampered_plan_is_rejected(field, value):
    source = thread_with()
    prepared = await plan(source)
    with pytest.raises(KernelError, match="Session事实"):
        apply(source, CompactionPlanned(plan=prepared.plan.model_copy(update={field: value})))


@pytest.mark.parametrize("state", ["accepted", "calling_model", "cancelling"])
async def test_plan_rejects_wrong_turn_phase(state):
    source = thread_with()
    prepared = await plan(source)
    with pytest.raises(KernelError):
        apply(change_turn(source, status=TurnStatus(state)), CompactionPlanned(plan=prepared.plan))


@pytest.mark.parametrize("boundary", ["foreign_turn", "foreign_thread", "expired", "future"])
async def test_plan_rejects_identity_or_time_mismatch(boundary):
    source = thread_with()
    prepared = await plan(source)
    fields = {}
    if boundary.startswith("foreign"):
        prepared_plan = prepared.plan.model_copy(
            update={"turn_id" if boundary == "foreign_turn" else "thread_id": uuid4()}
        )
    else:
        prepared_plan = prepared.plan
        fields["occurred_at"] = source.turns[-1].created_at + timedelta(
            seconds=120 if boundary == "expired" else -1
        )
    with pytest.raises(KernelError):
        apply(source, CompactionPlanned(plan=prepared_plan), **fields)


@pytest.mark.parametrize(
    "payload",
    [
        ItemStarted(item_id=uuid4(), content=TextContent(kind="assistant_message", text="注入")),
        TurnStateChanged(status=TurnStatus.CALLING_MODEL),
        TurnStateChanged(status=TurnStatus.INTERRUPTED, error=FAILURE),
        ModelAttemptStarted(
            attempt_id=uuid4(), step=1, index=1, provider="test", requested_model="m"
        ),
    ],
)
async def test_open_ledger_blocks_unrelated_source_mutations(payload):
    _, _, thread = await planned()
    with pytest.raises(KernelError, match="开放压缩"):
        apply(thread, payload)


@pytest.mark.parametrize("fields", [{"step": 2}, {"index": 2}])
async def test_summary_attempt_cannot_change_step_or_retry(fields):
    _, _, thread = await planned()
    with pytest.raises(KernelError):
        start(thread, **fields)
    running = start(thread)
    with pytest.raises(KernelError, match="单次"):
        start(running)


async def test_attempt_id_unique_across_generation_and_compaction():
    _, _, thread = await planned()
    attempt = ModelAttempt(
        attempt_id=uuid4(),
        step=1,
        index=1,
        provider="test",
        requested_model="m",
        started_at=thread.created_at,
        finished_at=thread.created_at,
        status="failed",
        error=FAILURE,
    )
    thread = change_turn(thread, model_attempts=(attempt,))
    with pytest.raises(KernelError, match="Thread内重复"):
        start(thread, attempt_id=attempt.attempt_id)


async def test_compaction_id_and_target_step_cannot_be_reused_after_failure():
    source, prepared, thread = await planned()
    thread = reject(thread)
    for ident in (prepared.plan.compaction_id, uuid4()):
        updated = await plan(thread, identity=ident)
        with pytest.raises(KernelError):
            apply(thread, CompactionPlanned(plan=updated.plan))
    assert thread.turns[-1].items == source.turns[-1].items


@pytest.mark.parametrize(
    "boundary",
    ["unknown_finish", "reject_running", "usage_backslide", "model_drift", "response_drift"],
)
async def test_accounting_invariants_cannot_be_bypassed(boundary):
    _, _, thread = await planned()
    thread = start(thread)
    with pytest.raises(KernelError):
        if boundary == "unknown_finish":
            finish(thread)
        elif boundary == "reject_running":
            reject(thread)
        else:
            thread = observation(thread)
            if boundary == "usage_backslide":
                observation(thread, usage=UsageObservation(completeness="partial", input_tokens=1))
            elif boundary == "model_drift":
                observation(thread, actual_model="other")
            else:
                observation(thread, response_id="other")


async def test_failed_candidate_retains_successful_request_and_usage():
    source, prepared, thread = await planned()
    thread = finish(observation(start(thread)))
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    with pytest.raises(KernelError, match="指纹"):
        apply(
            thread,
            CompactionSummarized(
                compaction_id=prepared.plan.compaction_id,
                summary=summary(prepared),
                candidate_history_sha256="0" * 64,
                candidate_history_tokens=candidate.history_tokens,
            ),
        )
    thread = reject(thread)
    record = thread.turns[-1].compactions[-1]
    assert record.status == "failed" and record.attempt.status == "completed"
    assert record.summary is None
    assert thread.turns[-1].usage.total_tokens == 130
    with pytest.raises(KernelError):
        observation(thread)


async def test_cancelling_allows_settlement_but_never_new_attempt():
    _, _, thread = await planned()
    cancelled = apply(thread, TurnStateChanged(status=TurnStatus.CANCELLING))
    with pytest.raises(KernelError, match="取消"):
        start(cancelled)
    assert reject(cancelled, "cancelled").turns[-1].compactions[-1].attempt is None
    running = apply(start(thread), TurnStateChanged(status=TurnStatus.CANCELLING))
    running = observation(running, complete=False)
    terminal = reject(finish(running, "cancelled"), "cancelled")
    assert terminal.turns[-1].usage == Usage(input_tokens=100)
    assert terminal.turns[-1].compactions[-1].attempt.usage.completeness == "partial"


async def test_all_compaction_events_require_v14():
    source, prepared, thread = await planned()
    thread = start(thread)
    record = thread.turns[-1].compactions[-1]
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    payloads = (
        CompactionPlanned(plan=prepared.plan),
        CompactionAttemptStarted(
            compaction_id=prepared.plan.compaction_id,
            event=ModelAttemptStarted(
                attempt_id=record.attempt.attempt_id,
                step=1,
                index=1,
                provider="test",
                requested_model="m",
            ),
        ),
        CompactionUsageObserved(
            compaction_id=prepared.plan.compaction_id,
            event=ModelUsageObserved(
                attempt_id=record.attempt.attempt_id, usage=UsageObservation()
            ),
        ),
        CompactionAttemptFinished(
            compaction_id=prepared.plan.compaction_id,
            event=ModelAttemptFinished(
                attempt_id=record.attempt.attempt_id, outcome="failed", error=FAILURE
            ),
        ),
        CompactionRejected(
            compaction_id=prepared.plan.compaction_id, outcome="failed", failure=FAILURE
        ),
        CompactionSummarized(
            compaction_id=prepared.plan.compaction_id,
            summary=summary(prepared),
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ),
    )
    for payload in payloads:
        for version in range(1, 14):
            with pytest.raises(ValidationError):
                EventDraft(schema_version=version, payload=payload)
        event = EventDraft(payload=payload)
        assert EventDraft.model_validate_json(event.model_dump_json()) == event


async def test_frozen_decisions_replay_after_ledger_sequence_advances():
    source, prepared, thread = await planned()
    assert prepared.model_history.new_decisions
    assert thread.turns[-1].tool_result_view_decisions == prepared.model_history.new_decisions
    assert not thread.turns[-1].model_history_inspections
    with pytest.raises(KernelError, match="不一致"):
        apply(source, CompactionPlanned(plan=prepared.plan, decisions=()))
    with pytest.raises(KernelError, match="准备状态"):
        await plan(thread)
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    thread = finish(observation(start(thread)))
    for field, value in (
        ("summary", summary(prepared, "x" * 2000)),
        ("candidate_history_tokens", candidate.history_tokens + 1),
    ):
        payload = CompactionSummarized(
            compaction_id=prepared.plan.compaction_id,
            summary=summary(prepared),
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ).model_copy(update={field: value})
        with pytest.raises(KernelError):
            apply(thread, payload)


@pytest.mark.parametrize("stage", ["planned", "sampling", "summarized"])
async def test_record_validation_rejects_phase_or_time_forgery(stage):
    _, _, thread = await planned()
    if stage != "planned":
        thread = start(thread)
    if stage == "summarized":
        thread = finish(observation(thread))
    record = thread.turns[-1].compactions[-1]
    with pytest.raises(ValidationError):
        CompactionRecord.model_validate(record.model_dump() | {"status": "summarized"})
    with pytest.raises(ValidationError):
        CompactionRecord.model_validate(
            record.model_dump()
            | {
                "finished_at": record.created_at - timedelta(seconds=1),
            }
        )


async def test_deadline_and_budget_exhaustion_allow_settlement_not_new_request():
    _, _, thread = await planned()
    record = thread.turns[-1].compactions[-1]
    event = CompactionAttemptStarted(
        compaction_id=record.plan.compaction_id,
        event=ModelAttemptStarted(
            attempt_id=uuid4(), step=1, index=1, provider="test", requested_model="m"
        ),
    )
    with pytest.raises(KernelError, match="时间"):
        apply(thread, event, occurred_at=thread.turns[-1].created_at + timedelta(seconds=120))
    exhausted = change_turn(thread, usage=Usage(input_tokens=100_000))
    with pytest.raises(KernelError, match="预算"):
        apply(exhausted, event)
    running = observation(
        start(thread),
        usage=UsageObservation(
            completeness="complete",
            input_tokens=100_000,
            output_tokens=50,
        ),
    )
    rejected = reject(finish(running))
    assert rejected.turns[-1].usage.total_tokens == 100_050  # 不截断已发生的实际用量。
    with pytest.raises(KernelError, match="预算"):
        apply(rejected, TurnStateChanged(status=TurnStatus.CALLING_MODEL))


async def test_generation_cannot_reuse_summary_attempt_identity():
    _, _, thread = await planned()
    thread = reject(finish(observation(start(thread))))
    thread = apply(thread, TurnStateChanged(status=TurnStatus.CALLING_MODEL))
    with pytest.raises(KernelError, match="Thread 内重复"):
        apply(
            thread,
            ModelAttemptStarted(
                attempt_id=thread.turns[-1].compactions[-1].attempt.attempt_id,
                step=1,
                index=1,
                provider="test",
                requested_model="m",
            ),
        )


async def test_provider_without_persisted_attempt_intent_keeps_unknown_charge_risk():
    _, _, thread = await planned()
    record = thread.turns[-1].compactions[-1]
    thread = apply(
        thread,
        CompactionRejected(
            compaction_id=record.plan.compaction_id,
            outcome="failed",
            failure=AgentFailure(
                code="summary_provider_accounting_required", message="摘要Provider未先提交请求意图"
            ),
            unaccounted_request_possible=True,
        ),
    )
    record = thread.turns[-1].compactions[-1]
    assert record.attempt is None and record.unaccounted_request_possible
    with pytest.raises(ValidationError):
        CompactionRecord.model_validate(
            record.model_dump() | {"status": "cancelled", "unaccounted_request_possible": True}
        )
