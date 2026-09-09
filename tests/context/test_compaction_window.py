from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    CompactionWindowActivated,
    EventDraft,
    ModelHistoryPrepared,
    Thread,
)
from harnessix.agent.reducer import apply_event
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.context.compaction import validate_compaction
from harnessix.context.compaction_ledger_contracts import (
    CompactionAttemptFinished,
    CompactionAttemptStarted,
    CompactionPlanned,
    CompactionSummarized,
    CompactionUsageObserved,
)
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.compaction_window import (
    build_compaction_window,
    prepare_active_model_history,
)
from harnessix.context.tool_result_contracts import (
    ModelHistoryInspectionV2,
    ToolResultViewPolicy,
)
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.context.compaction_ledger_helpers import append, prepare_source
from tests.context.test_compaction import plan, summary, tool_group
from tests.context.test_compaction_ledger import apply, finish, observation, planned, start


async def summarized_window_source(thread=None):
    source, prepared, ledger = await planned(thread)
    candidate = await validate_compaction(source, prepared.plan, summary(prepared), CancelToken())
    ledger = finish(observation(start(ledger)))
    ledger = apply(
        ledger,
        CompactionSummarized(
            compaction_id=prepared.plan.compaction_id,
            summary=summary(prepared),
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ),
    )
    return source, prepared, candidate, ledger


def window_event(thread, candidate, *, window_id=None):
    record = thread.turns[-1].compactions[-1]
    occurred_at = thread.updated_at + timedelta(microseconds=1)
    window = build_compaction_window(
        thread,
        record,
        candidate,
        window_id=window_id or uuid4(),
        activated_event_sequence=thread.sequence + 1,
        activated_at=occurred_at,
    )
    return AgentEvent(
        thread_id=thread.thread_id,
        turn_id=thread.active_turn_id,
        sequence=thread.sequence + 1,
        occurred_at=occurred_at,
        payload=CompactionWindowActivated(window=window),
    )


async def test_window_activation_keeps_raw_facts_and_drives_v2_history():
    source, prepared, candidate, ledger = await summarized_window_source()
    event = window_event(ledger, candidate)
    active = apply_event(ledger, event)

    assert active.compaction_windows == (event.payload.window,)
    assert active.active_compaction_window_id == event.payload.window.window_id
    assert active.turns[-1].items == source.turns[-1].items
    assert event.payload.window.raw_history_items == len(source.turns[-1].items)
    assert event.payload.window.history_item_ids == tuple(
        item.item_id for item in candidate.history
    )

    model_history = prepare_active_model_history(
        active, prepared.plan.model_step, prepared.plan.tool_result_view_policy
    )
    assert model_history.history == candidate.history
    assert not model_history.new_decisions
    assert isinstance(model_history.inspection, ModelHistoryInspectionV2)
    assert model_history.inspection.window_id == event.payload.window.window_id
    assert model_history.inspection.window_history_sha256 == candidate.history_sha256

    frozen = apply(
        active,
        ModelHistoryPrepared(
            inspection=model_history.inspection,
            decisions=model_history.new_decisions,
        ),
    )
    assert frozen.turns[-1].model_history_inspections == (model_history.inspection,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("previous_window_id", uuid4()),
        ("history_sha256", "0" * 64),
        ("history_tokens", 1),
        ("raw_history_items", 1),
        ("raw_history_ids_sha256", "0" * 64),
        ("model_step", 2),
    ],
)
async def test_window_payload_must_equal_replayed_candidate(field, value):
    _, _, candidate, ledger = await summarized_window_source()
    event = window_event(ledger, candidate)
    forged = event.model_copy(
        update={
            "payload": CompactionWindowActivated(
                window=event.payload.window.model_copy(update={field: value})
            )
        }
    )
    with pytest.raises(KernelError, match="不一致"):
        apply_event(ledger, forged)


async def test_window_must_immediately_follow_candidate_and_precede_history_freeze():
    _, prepared, candidate, ledger = await summarized_window_source()
    raw = prepare_active_model_history(
        ledger, prepared.plan.model_step, prepared.plan.tool_result_view_policy
    )
    advanced = apply(
        ledger,
        ModelHistoryPrepared(inspection=raw.inspection, decisions=raw.new_decisions),
    )
    stale = window_event(ledger, candidate).model_copy(update={"sequence": advanced.sequence + 1})
    with pytest.raises(KernelError, match="冻结|最新"):
        apply_event(advanced, stale)


async def test_window_contract_requires_v15_and_linear_active_tail():
    _, prepared, candidate, ledger = await summarized_window_source()
    event = window_event(ledger, candidate)
    active = apply_event(ledger, event)
    history = prepare_active_model_history(
        active, prepared.plan.model_step, prepared.plan.tool_result_view_policy
    )

    with pytest.raises(ValidationError):
        EventDraft(schema_version=14, payload=event.payload)
    with pytest.raises(ValidationError):
        EventDraft(
            schema_version=14,
            payload=ModelHistoryPrepared(inspection=history.inspection),
        )
    assert EventDraft(payload=event.payload).schema_version == 18
    assert (
        EventDraft(payload=ModelHistoryPrepared(inspection=history.inspection)).schema_version == 18
    )
    with pytest.raises(ValidationError):
        Thread.model_validate(active.model_dump() | {"active_compaction_window_id": None})


async def test_active_window_detects_raw_prefix_identity_change():
    _, prepared, candidate, ledger = await summarized_window_source()
    active = apply_event(ledger, window_event(ledger, candidate))
    turn = active.turns[-1]
    changed = turn.items[0].model_copy(update={"item_id": uuid4()})
    corrupted = active.model_copy(
        update={"turns": (turn.model_copy(update={"items": (changed, *turn.items[1:])}),)}
    )
    with pytest.raises(KernelError, match="前缀"):
        prepare_active_model_history(
            corrupted, prepared.plan.model_step, prepared.plan.tool_result_view_policy
        )


async def test_repeated_compaction_uses_prior_window_plus_raw_delta_only():
    _, first, first_candidate, ledger = await summarized_window_source()
    active = apply_event(ledger, window_event(ledger, first_candidate))
    turn = active.turns[-1]
    delta = (*tool_group(), *tool_group())
    extended = active.model_copy(
        update={
            "sequence": active.sequence + 1,
            "updated_at": active.updated_at + timedelta(microseconds=1),
            "turns": (
                turn.model_copy(
                    update={
                        "items": (*turn.items, *delta),
                        "model_steps": 1,
                        "usage_step": 1,
                    }
                ),
            ),
        }
    )
    second = await plan(extended, target_history_tokens=8000)
    first_summary_id = compaction_summary_item(summary(first)).item_id
    assert first_summary_id in second.plan.source_item_ids
    assert not set(first.plan.covered_item_ids) & set(second.plan.source_item_ids)
    assert {item.item_id for item in delta} <= set(second.plan.source_item_ids)

    second_candidate = await validate_compaction(
        extended,
        second.plan,
        summary(second, "第二窗口摘要，保留接口与未完成任务。"),
        CancelToken(),
    )
    second_summary = summary(second, "第二窗口摘要，保留接口与未完成任务。")
    second_ledger = apply(
        extended,
        CompactionPlanned(
            plan=second.plan,
            decisions=second.model_history.new_decisions,
        ),
    )
    second_ledger = finish(observation(start(second_ledger)))
    second_ledger = apply(
        second_ledger,
        CompactionSummarized(
            compaction_id=second.plan.compaction_id,
            summary=second_summary,
            candidate_history_sha256=second_candidate.history_sha256,
            candidate_history_tokens=second_candidate.history_tokens,
        ),
    )
    second_event = window_event(second_ledger, second_candidate)
    twice = apply_event(second_ledger, second_event)

    assert len(twice.compaction_windows) == 2
    assert twice.compaction_windows[-1].previous_window_id == active.active_compaction_window_id
    assert twice.active_compaction_window_id == second_event.payload.window.window_id
    final = prepare_active_model_history(twice, second.plan.model_step, ToolResultViewPolicy())
    assert final.history == second_candidate.history
    assert isinstance(final.inspection, ModelHistoryInspectionV2)


async def test_reopen_activates_committed_candidate_without_provider_request(tmp_path):
    store = SQLiteSessionStore(tmp_path / "recovery.sqlite")
    source, prepared = await prepare_source(store, tmp_path)
    compaction_id = prepared.plan.compaction_id
    attempt = ModelAttemptStarted(
        attempt_id=uuid4(),
        step=prepared.plan.model_step,
        index=1,
        provider="fixture",
        requested_model="summary-model",
    )
    compaction_summary = summary(prepared)
    candidate = await validate_compaction(source, prepared.plan, compaction_summary, CancelToken())
    persisted = await append(
        store,
        source,
        CompactionPlanned(plan=prepared.plan, decisions=prepared.model_history.new_decisions),
        CompactionAttemptStarted(compaction_id=compaction_id, event=attempt),
        CompactionUsageObserved(
            compaction_id=compaction_id,
            event=ModelUsageObserved(
                attempt_id=attempt.attempt_id,
                usage=UsageObservation(completeness="complete", input_tokens=10, output_tokens=3),
                actual_model="summary-model",
                response_id="summary-response",
            ),
        ),
        CompactionAttemptFinished(
            compaction_id=compaction_id,
            event=ModelAttemptFinished(attempt_id=attempt.attempt_id, outcome="completed"),
        ),
        CompactionSummarized(
            compaction_id=compaction_id,
            summary=compaction_summary,
            candidate_history_sha256=candidate.history_sha256,
            candidate_history_tokens=candidate.history_tokens,
        ),
    )
    assert persisted.sequence == persisted.turns[-1].compactions[-1].finished_event_sequence

    provider = FakeProvider()
    async with AgentRuntime(store, provider):
        recovered = await store.get_thread(source.thread_id)

    turn = recovered.turns[-1]
    assert turn.status == "interrupted"
    assert len(recovered.compaction_windows) == 1
    assert recovered.compaction_windows[0].compaction_id == compaction_id
    assert not provider.requests
    events = await store.events(source.thread_id)
    summarized_index = next(
        index for index, event in enumerate(events) if event.payload.type == "compaction_summarized"
    )
    assert events[summarized_index + 1].payload.type == "compaction_window_activated"
