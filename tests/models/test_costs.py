import json

import pytest

from harnessix.agent.usage import UsageObservation
from harnessix.models.costs import CostReport, bind_price, build_cost_report
from tests.models.pricing_helpers import attempt, context, price, turn


def report_for(*attempts, steps=1, status="completed"):
    return build_cost_report(
        turn(*attempts, steps=steps, status=status),
        tuple(bind_price(a, price(), context()) for a in attempts),
    )


def test_retries_count_attempts_not_usage_observations():
    first, second = attempt(status="failed"), attempt(index=2)
    report = report_for(first, second)
    assert report.summary.completeness == "complete"
    assert report.summary.totals[0].known_amount == "0.00005"
    assert CostReport.model_validate_json(report.model_dump_json()) == report


def test_unknown_retry_keeps_known_subtotal_incomplete():
    first = attempt(status="failed", usage=UsageObservation())
    report = report_for(first, attempt(index=2))
    assert report.summary.completeness == "partial"
    assert report.entries[0].result.amount is None
    assert report.summary.totals[0].known_amount == "0.000025"


@pytest.mark.parametrize("status", ["accepted", "calling_model", "waiting_approval", "finalizing"])
def test_running_turn_cannot_claim_final_cost(status):
    report = report_for(attempt(), status=status)
    assert report.summary.completeness == "partial"
    assert report.summary.totals[0].known_amount == "0.000025"


@pytest.mark.parametrize("steps", [0, 1, 3])
def test_no_attempts_and_legacy_steps_are_unknown(steps):
    report = build_cost_report(turn(steps=steps))
    assert report.summary.completeness == "unknown" and not report.summary.totals
    assert report.summary.uncovered_steps == tuple(range(1, steps + 1))


def test_mixed_legacy_step_is_not_hidden_by_priced_step():
    report = report_for(attempt(step=2), steps=2)
    assert report.summary.completeness == "partial" and report.summary.uncovered_steps == (1,)


def test_currencies_are_separate_and_binding_order_is_irrelevant():
    a, b = attempt(), attempt(step=2)
    bindings = (bind_price(a, price(), context()), bind_price(b, price(currency="CNY"), context()))
    source = turn(a, b, steps=2)
    report = build_cost_report(source, bindings)
    assert report.summary.completeness == "complete"
    assert [(s.currency, s.known_amount) for s in report.summary.totals] == [
        ("CNY", "0.000025"),
        ("USD", "0.000025"),
    ]
    assert report == build_cost_report(source, tuple(reversed(bindings)))


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_binding",
        "extra_binding",
        "duplicate_attempt",
        "index_gap",
        "step_outside",
        "retry_after_success",
    ],
)
def test_invalid_report_membership_fails_closed(case):
    source = attempt()
    binding = bind_price(source, price(), context())
    values, bindings, steps = (source,), (binding,), 1
    if case == "duplicate_binding":
        bindings = (binding, binding)
    elif case == "extra_binding":
        bindings = (bind_price(attempt(), price(), context()),)
    elif case == "duplicate_attempt":
        values = (source, source)
    elif case == "index_gap":
        values, bindings = (attempt(index=2),), ()
    elif case == "step_outside":
        steps = 0
    else:
        values, bindings = (source, attempt(index=2)), ()
    with pytest.raises(ValueError):
        build_cost_report(turn(*values, steps=steps), bindings)


@pytest.mark.parametrize(
    "target", ["total", "amount", "line", "rate", "usage", "context", "unknown", "version"]
)
def test_report_json_recomputes_instead_of_trusting_stored_amount(target):
    data = json.loads(report_for(attempt()).model_dump_json())
    entry = data["entries"][0]
    if target == "total":
        data["summary"]["totals"][0]["known_amount"] = "999"
    elif target == "amount":
        entry["result"]["amount"] = "0"
    elif target == "line":
        entry["result"]["lines"][0]["tokens"] = 2
    elif target == "rate":
        entry["binding"]["price"]["output_per_million"] = "1"
    elif target == "usage":
        entry["attempt"]["usage"]["output_tokens"] = 3
    elif target == "context":
        entry["binding"]["context"]["region"] = "different"
    elif target == "unknown":
        data["secret"] = "must-not-be-accepted"
    else:
        data["spec_version"] = "harnessix.cost-report/v2"
    with pytest.raises(ValueError):
        CostReport.model_validate_json(json.dumps(data))


def test_repricing_creates_new_report_without_rewriting_old_snapshot():
    source = attempt()
    original = report_for(source)
    encoded = original.model_dump_json()
    newer = build_cost_report(
        turn(source),
        (bind_price(source, price(version="fixture-v2", output_per_million="8"), context()),),
    )
    assert newer.summary.totals[0].known_amount == "0.000033"
    assert original.model_dump_json() == encoded
    assert CostReport.model_validate_json(encoded).summary.totals[0].known_amount == "0.000025"
