from __future__ import annotations

import asyncio
import json

import pytest

from harnessix.agent.billing import ResponseBillingMetadata
from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelUsageObserved, UsageObservation
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed, prepare_attempt
from tests.models.billing_helpers import billing_frames
from tests.models.test_attempt_usage import adapter as adapter
from tests.models.test_attempt_usage import execute


@pytest.mark.parametrize("field", ["cache_creation_5m_tokens", "cache_creation_1h_tokens"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "1", float("inf")])
def test_strict_ttl_counts(field, value):
    with pytest.raises(ValueError):
        ResponseBillingMetadata.model_validate({field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"service_tier": ""},
        {"inference_geo": "secret?token=x"},
        {"service_tier": 1},
        {"headers": {"key": "canary"}},
    ],
)
def test_labels_and_extra_fields_are_bounded(change):
    with pytest.raises(ValueError):
        ResponseBillingMetadata.model_validate(change)


def test_unknown_zero_and_successor_are_distinct():
    unknown = ResponseBillingMetadata()
    zero = ResponseBillingMetadata(cache_creation_5m_tokens=0)
    assert not unknown.observed and zero.observed
    zero.validate_successor(unknown)
    with pytest.raises(ValueError):
        unknown.validate_successor(zero)
    for field in ("service_tier", "inference_geo"):
        before = ResponseBillingMetadata.model_validate({field: "first"})
        with pytest.raises(ValueError):
            ResponseBillingMetadata.model_validate({field: "second"}).validate_successor(before)
    with pytest.raises(ValueError):
        zero.validate_successor(ResponseBillingMetadata(cache_creation_5m_tokens=1))


def test_ttl_subsets_cannot_exceed_total():
    start = attempt_start()
    with pytest.raises(ValueError):
        ModelUsageObserved(
            attempt_id=start.attempt_id,
            usage=UsageObservation(completeness="partial", cache_creation_input_tokens=3),
            billing=ResponseBillingMetadata(cache_creation_5m_tokens=2, cache_creation_1h_tokens=2),
        )


def test_metadata_requires_v5_but_old_event_export_unchanged():
    start = attempt_start()
    payload = observed(start)
    assert "billing" not in EventDraft(schema_version=4, payload=payload).model_dump()["payload"]
    with pytest.raises(ValueError):
        EventDraft(
            schema_version=4,
            payload=payload.model_copy(
                update={"billing": ResponseBillingMetadata(service_tier="default")}
            ),
        )
    assert EventDraft(payload=payload).schema_version == 7


def test_legacy_event_projection_can_exclude_payload():
    event = EventDraft(schema_version=4, payload=observed(attempt_start()))
    assert "payload" not in event.model_dump(exclude={"payload"})
    assert event.model_dump(include={"schema_version"}) == {"schema_version": 4}


async def test_usage_and_metadata_commit_atomically_and_omission_keeps_fact(tmp_path):
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepare_attempt(store, tmp_path)
    start = attempt_start()
    first = observed(start, completeness="partial", input_tokens=10, output_tokens=1)
    first = first.model_copy(update={"billing": ResponseBillingMetadata(service_tier="standard")})
    thread = await store.append(
        thread.thread_id,
        [EventDraft(turn_id=thread.active_turn_id, payload=p) for p in [start, first]],
        expected_sequence=thread.sequence,
    )
    invalid = observed(start, completeness="complete", input_tokens=10, output_tokens=5).model_copy(
        update={"billing": ResponseBillingMetadata(service_tier="priority")}
    )
    with pytest.raises(KernelError, match="计费元数据冲突"):
        await store.append(
            thread.thread_id,
            [EventDraft(turn_id=thread.active_turn_id, payload=invalid)],
            expected_sequence=thread.sequence,
        )
    assert await store.get_thread(thread.thread_id) == thread
    next_observation = observed(start, completeness="complete", input_tokens=10, output_tokens=2)
    updated = await store.append(
        thread.thread_id,
        [EventDraft(turn_id=thread.active_turn_id, payload=next_observation)],
        expected_sequence=thread.sequence,
    )
    assert updated.turns[0].model_attempts[0].billing.service_tier == "standard"
    assert updated.turns[0].usage.output_tokens == 2


async def test_sdk_metadata_persisted_with_same_attempt_and_replay(adapter, tmp_path):
    turn, events, _ = await execute(adapter, tmp_path, billing_frames(adapter))
    assert turn.status == "completed" and turn.usage.total_tokens == 12
    attempt = turn.model_attempts[0]
    metadata = attempt.billing
    assert metadata.service_tier == ("default" if adapter.kind == "openai" else "standard")
    assert metadata.inference_geo == (None if adapter.kind == "openai" else "us")
    assert metadata.cache_creation_5m_tokens == (None if adapter.kind == "openai" else 3)
    facts = [
        e.payload for e in events if isinstance(e.payload, ModelUsageObserved) and e.payload.billing
    ]
    assert all(
        f.attempt_id == attempt.attempt_id
        and f.response_id == attempt.response_id
        and f.actual_model == attempt.actual_model
        for f in facts
    )


async def test_missing_billing_not_inferred_from_sdk_or_request(adapter, tmp_path):
    turn, _, _ = await execute(adapter, tmp_path, adapter.detailed_frames())
    assert turn.status == "completed" and not turn.model_attempts[0].billing.observed


async def test_failed_stream_keeps_metadata(adapter, tmp_path):
    turn, _, _ = await execute(adapter, tmp_path, billing_frames(adapter)[:-1])
    assert turn.status == "failed" and turn.model_attempts[0].billing.observed
    assert turn.model_attempts[0].billing.service_tier is not None


async def test_sdk_task_cancel_keeps_committed_billing(adapter, tmp_path):
    wire = adapter.wire.WireStream(billing_frames(adapter)[:1], block=True)
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with adapter.provider(wire) as provider:
        async with AgentRuntime(store, provider) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "取消计费观测", request_id="r")
            )
            await asyncio.wait_for(wire.entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    attempt = (await store.get_thread(thread.thread_id)).turns[0].model_attempts[0]
    assert attempt.status == "cancelled" and attempt.billing.observed and wire.closed


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
async def test_chat_late_tier_fills_and_drift_fails(adapter, tmp_path):
    parts = adapter.detailed_frames()
    value = json.loads(parts[-2].decode().split("data: ")[1])
    value["service_tier"] = "priority"
    parts[-2] = adapter.wire.frame(value)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    assert turn.status == "completed" and turn.model_attempts[0].billing.service_tier == "priority"
    # 使用新数据库路径，避免 request_id 去重使第二次根本不执行。
    other = tmp_path / "other"
    await asyncio.to_thread(other.mkdir)
    parts[0] = billing_frames(adapter)[0]
    turn, _, _ = await execute(adapter, other, parts)
    assert turn.status == "failed" and turn.model_attempts[0].billing.service_tier == "default"


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
async def test_chat_auto_is_not_actual_tier(adapter, tmp_path):
    turn, _, _ = await execute(adapter, tmp_path, billing_frames(adapter, tier="auto"))
    assert turn.status == "completed" and turn.model_attempts[0].billing.service_tier is None


@pytest.mark.parametrize("short,long", [(2, 2), (-1, 4), (True, 2)])
@pytest.mark.parametrize("adapter", ["anthropic"], indirect=True)
async def test_anthropic_bad_ttl_counts_never_release_semantics(adapter, tmp_path, short, long):
    turn, _, _ = await execute(adapter, tmp_path, billing_frames(adapter, short=short, long=long))
    assert turn.status == "failed"
    assert not turn.model_attempts[0].billing.observed


@pytest.mark.parametrize("adapter", ["anthropic"], indirect=True)
async def test_anthropic_late_detail_fills(adapter, tmp_path):
    parts = adapter.detailed_frames()
    parts[-2:] = adapter.wire.stop(
        service_tier="standard",
        inference_geo="us",
        cache_creation={"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 0},
    )
    turn, _, _ = await execute(adapter, tmp_path, parts)
    assert (
        turn.status == "completed" and turn.model_attempts[0].billing.cache_creation_5m_tokens == 3
    )


async def test_unchanged_usage_and_metadata_do_not_append_another_observation(adapter, tmp_path):
    parts = billing_frames(adapter)
    if adapter.kind == "openai":
        parts.insert(1, parts[0])
    else:
        parts.insert(
            -2,
            adapter.wire.frame(
                "message_delta",
                delta={"stop_reason": None, "stop_sequence": None},
                usage={
                    "output_tokens": 1,
                    "service_tier": "standard",
                    "inference_geo": "us",
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 3,
                        "ephemeral_1h_input_tokens": 0,
                    },
                },
            ),
        )
    turn, events, _ = await execute(adapter, tmp_path, parts)
    observations = [e.payload for e in events if isinstance(e.payload, ModelUsageObserved)]
    assert turn.status == "completed" and turn.usage.total_tokens == 12
    assert len(observations) == (2 if adapter.kind == "openai" else 3)


@pytest.mark.parametrize("adapter", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        {"service_tier": "priority"},
        {"inference_geo": "global"},
        {"cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 0}},
    ],
)
async def test_anthropic_drift_preserves_previous_metadata_and_usage(adapter, tmp_path, change):
    parts = billing_frames(adapter)
    parts[-2:] = adapter.wire.stop(**change)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    recorded = turn.model_attempts[0]
    assert turn.status == "failed" and recorded.usage.output_tokens == 1
    assert recorded.billing == ResponseBillingMetadata(
        service_tier="standard",
        inference_geo="us",
        cache_creation_5m_tokens=3,
        cache_creation_1h_tokens=0,
    )
