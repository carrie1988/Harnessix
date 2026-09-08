from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.models import TurnStatus
from harnessix.agent.usage import UsageObservation
from harnessix.models.costs import (
    COST_REPORT_ADAPTER,
    CostReport,
    CostReportV2,
    bind_price,
    build_cost_report,
)
from tests.context.test_compaction_ledger import finish, observation, planned, reject, start
from tests.models.pricing_helpers import attempt, context, price, turn


async def mixed_turn(*, complete=True):
    _, _, thread = await planned()
    thread = start(thread)
    thread = observation(thread, complete=complete)
    thread = reject(finish(thread, "completed" if complete else "interrupted"))
    source = thread.turns[-1]
    generation = attempt(
        started_at=source.created_at, finished_at=source.created_at + timedelta(seconds=1)
    )
    return source.model_copy(
        update={
            "model_attempts": (generation,),
            "model_steps": 1,
            "status": TurnStatus.COMPLETED,
        }
    )


def bindings(source, *, currency="USD", summary_model="summary-model"):
    return tuple(
        bind_price(
            a,
            price(
                model=summary_model if i else "test-model",
                currency=currency if i else "USD",
                **({"input_price": {"kind": "flat", "per_million": "2"}} if i else {}),
            ),
            context(),
        )
        for i, a in enumerate(source.accounted_attempts)
    )


async def test_generation_and_summary_index_one_are_distinct_fully_accounted_requests():
    source = await mixed_turn()
    report = build_cost_report(source, bindings(source))
    assert isinstance(report, CostReportV2)
    assert [(e.purpose, e.attempt.step, e.attempt.index) for e in report.entries] == [
        ("generation", 1, 1),
        ("compaction", 1, 1),
    ]
    assert report.summary.completeness == "complete"
    assert report.summary.totals[0].known_amount == "0.000345"
    assert report.compactions[0].status == "failed"  # 请求成功、候选失败不豁免费用。
    assert COST_REPORT_ADAPTER.validate_json(report.model_dump_json()) == report
    assert "summary-response" not in report.model_dump_json()
    assert "public" not in report.model_dump_json()
    assert isinstance(build_cost_report(turn(attempt())), CostReport)


@pytest.mark.parametrize(
    "case", ["unknown_usage", "unbound_price", "different_model", "generation_gap"]
)
async def test_missing_summary_cost_never_becomes_complete_zero(case):
    source = await mixed_turn(complete=case != "unknown_usage")
    bound = bindings(
        source, summary_model="other" if case == "different_model" else "summary-model"
    )
    if case == "unbound_price":
        bound = bound[:1]
    if case == "generation_gap":
        source = source.model_copy(update={"model_steps": 2})
    report = build_cost_report(source, bound)
    assert report.summary.completeness == "partial"
    if case == "generation_gap":
        assert report.summary.uncovered_steps == (2,)
    else:
        assert report.entries[-1].result.status == "unknown"
        assert report.entries[-1].result.amount is None
        assert report.summary.totals[0].known_amount == "0.000025"


async def test_summary_uses_own_currency_without_implicit_conversion():
    source = await mixed_turn()
    report = build_cost_report(source, bindings(source, currency="CNY"))
    assert report.summary.completeness == "complete"
    assert [(s.currency, s.known_amount) for s in report.summary.totals] == [
        ("CNY", "0.00032"),
        ("USD", "0.000025"),
    ]


@pytest.mark.parametrize("stage", ["planned", "running", "settled"])
async def test_open_compaction_keeps_cost_and_usage_incomplete(stage):
    _, _, thread = await planned()
    if stage != "planned":
        thread = start(thread)
    if stage == "settled":
        thread = finish(observation(thread))
    source = thread.turns[-1].model_copy(
        update={
            "model_attempts": (attempt(),),
            "model_steps": 1,
            "status": TurnStatus.COMPLETED,
        }
    )
    report = build_cost_report(source, bindings(source))
    assert not source.usage_is_complete
    assert report.summary.completeness == "partial"


async def test_unknown_interrupted_summary_does_not_hide_behind_complete_generation():
    source = await mixed_turn(complete=False)
    assert not source.usage_is_complete
    assert source.model_attempts[0].usage.completeness == "complete"
    source = source.model_copy(
        update={
            "compactions": (
                source.compactions[0].model_copy(
                    update={
                        "attempt": source.compactions[0].attempt.model_copy(
                            update={"usage": UsageObservation()}
                        )
                    }
                ),
            )
        }
    )
    report = build_cost_report(source, bindings(source))
    assert report.summary.completeness == "partial"


async def test_possible_unaccounted_request_prevents_complete_cost_claim():
    _, _, thread = await planned()
    record = thread.turns[-1].compactions[-1]
    thread = reject(thread).model_copy(deep=True)
    turn_state = thread.turns[-1]
    risky = turn_state.compactions[-1].model_copy(update={"unaccounted_request_possible": True})
    source = turn_state.model_copy(
        update={
            "compactions": (risky,),
            "model_attempts": (attempt(),),
            "model_steps": 1,
            "status": TurnStatus.COMPLETED,
        }
    )
    report = build_cost_report(source, bindings(source))
    assert report.compactions[0].compaction_id == record.plan.compaction_id
    assert report.compactions[0].unaccounted_request_possible
    assert report.summary.completeness == "partial"
    assert report.summary.totals[0].known_amount == "0.000025"


@pytest.mark.parametrize(
    "target",
    [
        "omit_summary",
        "erase_states",
        "wrong_id",
        "wrong_step",
        "purpose",
        "index",
        "duplicate_id",
        "duplicate_summary",
        "future_target",
        "successful_without_attempt",
        "subtotal",
    ],
)
async def test_report_v2_revalidates_membership_and_totals(target):
    source = await mixed_turn()
    report = build_cost_report(source, bindings(source))
    data = json.loads(report.model_dump_json())
    summary = data["entries"][-1]
    if target == "omit_summary":
        data["entries"].pop()
    elif target == "erase_states":
        data["compactions"] = []
    elif target == "wrong_id":
        summary["compaction_id"] = str(uuid4())
    elif target == "wrong_step":
        data["compactions"][0]["model_step"] = 2
    elif target == "purpose":
        summary["purpose"] = "generation"
    elif target == "index":
        summary["attempt"]["index"] = 2
    elif target == "duplicate_id":
        summary["attempt"]["attempt_id"] = data["entries"][0]["attempt"]["attempt_id"]
    elif target == "duplicate_summary":
        data["entries"].append(summary)
    elif target == "future_target":
        data["compactions"][0]["model_step"] = 3
    elif target == "successful_without_attempt":
        data["compactions"][0].update(status="summarized", attempt_id=None)
    else:
        data["summary"]["totals"][0]["known_amount"] = "0"
    with pytest.raises(ValidationError):
        CostReportV2.model_validate_json(json.dumps(data))
