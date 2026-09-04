import pytest
from pydantic import ValidationError

from harnessix.agent.ids import new_id
from harnessix.agent.models import EventDraft
from harnessix.agent.usage import ModelAttempt, ModelAttemptFinished, UsageObservation
from harnessix.domain.models import utc_now
from tests.agent.attempt_helpers import attempt_start, observed


@pytest.mark.parametrize(
    "values",
    [
        {"input_tokens": 0},
        {"completeness": "partial"},
        {"completeness": "complete", "input_tokens": 1},
        {"completeness": "complete", "output_tokens": 1},
        {"completeness": "partial", "input_tokens": -1},
        {"completeness": "partial", "input_tokens": True},
        {"completeness": "partial", "input_tokens": "1"},
        {"completeness": "partial", "input_tokens": 1.0},
        {"completeness": "partial", "input_tokens": 3, "cache_read_input_tokens": 4},
        {
            "completeness": "partial",
            "input_tokens": 3,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 2,
        },
        {
            "completeness": "partial",
            "input_tokens": 4,
            "uncached_input_tokens": 1,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 1,
        },
        {"completeness": "partial", "output_tokens": 3, "reasoning_output_tokens": 4},
    ],
)
def test_invalid_counts_fail_closed(values) -> None:
    with pytest.raises(ValidationError):
        UsageObservation.model_validate(values)


def test_unknown_zero_and_missing_details_are_distinct() -> None:
    assert UsageObservation().input_tokens is None
    zero = UsageObservation(completeness="complete", input_tokens=0, output_tokens=0)
    assert zero.cache_read_input_tokens is None
    assert zero != UsageObservation()
    assert UsageObservation.model_validate_json(zero.model_dump_json()) == zero
    assert UsageObservation(completeness="partial", cache_read_input_tokens=4).input_tokens is None
    UsageObservation(
        completeness="complete",
        input_tokens=9,
        output_tokens=3,
        uncached_input_tokens=2,
        cache_read_input_tokens=4,
        cache_creation_input_tokens=3,
        reasoning_output_tokens=2,
    )


@pytest.mark.parametrize(
    "next_values",
    [
        {},
        {"completeness": "partial", "input_tokens": 2},
        {"completeness": "partial", "input_tokens": 3},
        {"completeness": "partial", "input_tokens": 3, "output_tokens": 0},
        {"completeness": "complete", "input_tokens": 3, "output_tokens": 1},
    ],
)
def test_partial_snapshot_cannot_drop_or_rewind_counts(next_values) -> None:
    previous = UsageObservation(
        completeness="partial", input_tokens=3, output_tokens=1, cache_read_input_tokens=1
    )
    with pytest.raises(ValueError):
        UsageObservation(**next_values).validate_successor(previous)


def test_complete_usage_can_only_enrich_missing_details() -> None:
    previous = UsageObservation(completeness="complete", input_tokens=10, output_tokens=3)
    previous.validate_successor(previous)
    enriched = previous.model_copy(update={"cache_read_input_tokens": 4})
    enriched.validate_successor(previous)
    with pytest.raises(ValueError):
        previous.model_copy(update={"input_tokens": 11}).validate_successor(previous)
    with pytest.raises(ValueError):
        previous.model_copy(update={"completeness": "partial"}).validate_successor(previous)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_attempt_payloads_require_event_v4(version) -> None:
    start = attempt_start()
    for payload in [
        start,
        observed(start),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
    ]:
        with pytest.raises(ValidationError):
            EventDraft(schema_version=version, payload=payload)
        assert EventDraft(payload=payload).schema_version == 7


def test_attempt_snapshot_cannot_claim_success_without_receipt() -> None:
    start = attempt_start()
    attempt = ModelAttempt(**start.model_dump(exclude={"type"}), started_at=utc_now())
    data = attempt.model_dump()
    with pytest.raises(ValidationError):
        ModelAttempt.model_validate({**data, "status": "completed", "finished_at": utc_now()})
    with pytest.raises(ValidationError):
        ModelAttempt.model_validate({**data, "finished_at": utc_now()})
    with pytest.raises(ValidationError):
        ModelAttemptFinished(attempt_id=new_id(), outcome="failed")
