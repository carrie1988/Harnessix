from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import Usage
from harnessix.agent.usage import UsageObservation
from harnessix.context.compaction_ledger_contracts import CompactionRecord
from harnessix.evals.campaign import build_coding_eval_campaign_report
from harnessix.evals.report import eval_report_sha256
from harnessix.models.costs import bind_price, build_cost_report
from tests.context.test_compaction import plan as compaction_plan
from tests.context.test_compaction import thread_with
from tests.evals.test_campaign import evidence, plan
from tests.models.pricing_helpers import NOW, attempt, context, price


@pytest.mark.parametrize("case", ["complete", "unknown", "other_model", "omitted"])
async def test_campaign_counts_all_purposes_and_never_omits_summary_cost(case):
    run_id, second_run_id = uuid4(), uuid4()
    original = evidence(run_id)
    prepared = await compaction_plan(thread_with())
    summary = attempt(
        actual_model="other-model" if case == "other_model" else "test-model",
        status="interrupted" if case == "unknown" else "completed",
        **({"usage": UsageObservation()} if case == "unknown" else {}),
    )
    record = CompactionRecord(
        plan=prepared.plan.model_copy(update={"turn_id": original.turn.turn_id}),
        status="failed",
        input_tokens_before=0,
        output_tokens_before=0,
        created_event_sequence=prepared.plan.source_event_sequence + 1,
        finished_event_sequence=prepared.plan.source_event_sequence + 2,
        attempt=summary,
        created_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        failure=AgentFailure(code="context_compaction_summary_overflow", message="候选超过预算"),
    )
    usage = Usage(
        input_tokens=original.turn.usage.input_tokens + (summary.usage.input_tokens or 0),
        output_tokens=original.turn.usage.output_tokens + (summary.usage.output_tokens or 0),
    )
    turn = original.turn.model_copy(update={"compactions": (record,), "usage": usage})
    report = original.report.model_copy(
        update={
            "metrics": original.report.metrics.model_copy(
                update={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                }
            )
        }
    )
    current = replace(
        original,
        turn=turn,
        report=report,
        state=original.state.model_copy(update={"report_sha256": eval_report_sha256(report)}),
        cost=original.cost
        if case == "omitted"
        else build_cost_report(
            turn,
            tuple(bind_price(a, price(), context()) for a in turn.accounted_attempts),
        ),
    )
    if case == "omitted":
        with pytest.raises(KernelError, match="Turn事实"):
            build_coding_eval_campaign_report(
                plan(run_id, second_run_id), (current, evidence(second_run_id))
            )
        return
    campaign = build_coding_eval_campaign_report(
        plan(run_id, second_run_id), (current, evidence(second_run_id))
    )
    trial = campaign.trials[0]
    assert trial.model_attempts == len(original.turn.model_attempts) + 1
    assert trial.input_tokens == usage.input_tokens
    assert trial.output_tokens == usage.output_tokens
    assert trial.cost_completeness == ("complete" if case == "complete" else "partial")
    assert trial.known_cost_amount == ("0.000175" if case == "complete" else "0.00015")
    assert trial.actual_models == (
        ("other-model", "test-model") if case == "other_model" else ("test-model",)
    )
    assert "候选超过预算" not in campaign.model_dump_json()
