from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import TurnStatus, Usage
from harnessix.agent.usage import UsageObservation
from harnessix.domain.models import ContractModel
from harnessix.evals.campaign import (
    CompletedCodingEvalTrial,
    build_coding_eval_campaign_report,
)
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignPlan,
    CodingEvalCampaignReport,
)
from harnessix.evals.contracts import CodingEvalRunState, EvalTestObservation
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.report import (
    eval_report_sha256,
    read_eval_campaign_plan,
    read_eval_campaign_report,
    write_eval_campaign_plan,
    write_eval_campaign_report,
)
from harnessix.models.costs import bind_price, build_cost_report
from tests.evals.test_grader import (
    BASELINE_REVISION,
    completed_turn,
    environment,
    git_evidence,
    observations,
    task,
)
from tests.models.pricing_helpers import NOW, attempt, context, price


def campaign_environment():
    return environment().model_copy(update={"provider": "openai_chat", "model": "test-model"})


def plan(*run_ids: UUID) -> CodingEvalCampaignPlan:
    current_task = task()
    return CodingEvalCampaignPlan(
        campaign_id=uuid4(),
        task_id=current_task.task_id,
        task_version=current_task.task_version,
        task_fingerprint=current_task.fingerprint,
        environment=campaign_environment(),
        run_ids=run_ids,
        price=price(),
        billing_context=context(),
        created_at=NOW - timedelta(seconds=1),
    )


def _failed_final() -> tuple[EvalTestObservation, ...]:
    _, final = observations()
    return (
        final[0].model_copy(update={"passed": False, "returncode": 1}),
        final[1],
    )


def evidence(
    run_id: UUID,
    *,
    outcome: str = "passed",
    elapsed_seconds: float = 1,
    usage_complete: bool = True,
) -> CompletedCodingEvalTrial:
    current_task = task()
    baseline, final = observations()
    if outcome in {"provider", "runtime"}:
        error = AgentFailure(
            code="provider_transport" if outcome == "provider" else "internal_crash",
            message="不可进入Campaign报告的供应商原始错误标记",
            retryable=True,
        )
        model_attempt = attempt(
            status="failed",
            error=error,
            usage=UsageObservation(
                completeness="complete",
                input_tokens=10,
                output_tokens=2,
                uncached_input_tokens=3,
                cache_read_input_tokens=4,
                cache_creation_input_tokens=3,
                reasoning_output_tokens=1,
            )
            if usage_complete
            else UsageObservation(),
        )
        turn = completed_turn().model_copy(
            update={
                "status": TurnStatus.FAILED,
                "items": (),
                "model_steps": 1,
                "usage": Usage(input_tokens=10, output_tokens=2) if usage_complete else Usage(),
                "model_attempts": (model_attempt,),
                "error": error,
                "created_at": NOW,
                "completed_at": NOW + timedelta(seconds=1),
            }
        )
        final = _failed_final()
    else:
        attempts = tuple(attempt(step=step) for step in range(1, 7))
        turn = completed_turn().model_copy(
            update={
                "model_attempts": attempts,
                "usage": Usage(input_tokens=60, output_tokens=12),
                "created_at": NOW,
                "completed_at": NOW + timedelta(seconds=1),
            }
        )
        if outcome == "task":
            final = _failed_final()
    report = grade_coding_eval(
        current_task,
        turn,
        run_id=run_id,
        environment=campaign_environment(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=elapsed_seconds),
        baseline_observations=baseline,
        final_observations=final,
        git=git_evidence(),
    )
    state = CodingEvalRunState(
        run_id=run_id,
        task_id=current_task.task_id,
        task_version=current_task.task_version,
        task_fingerprint=current_task.fingerprint,
        baseline_revision=BASELINE_REVISION,
        baseline_tree_sha256=current_task.repository.baseline_tree_sha256,
        execution_workspace_id=uuid4(),
        status="completed",
        thread_id=uuid4(),
        turn_id=turn.turn_id,
        baseline_observations=baseline,
        environment=campaign_environment(),
        report_sha256=eval_report_sha256(report),
        started_at=NOW,
        updated_at=report.completed_at,
    )
    cost = build_cost_report(
        turn,
        tuple(bind_price(item, price(), context()) for item in turn.model_attempts),
    )
    return CompletedCodingEvalTrial(state=state, report=report, turn=turn, cost=cost)


def test_campaign_aggregates_independent_success_provider_and_task_trials() -> None:
    run_ids = (uuid4(), uuid4(), uuid4(), uuid4())
    report = build_coding_eval_campaign_report(
        plan(*run_ids),
        (
            evidence(run_ids[0]),
            evidence(run_ids[1], outcome="provider", elapsed_seconds=2),
            evidence(run_ids[2], outcome="runtime", elapsed_seconds=3),
            evidence(run_ids[3], outcome="task", elapsed_seconds=4),
        ),
    )

    assert [trial.classification for trial in report.trials] == [
        "passed",
        "provider",
        "runtime",
        "task",
    ]
    assert report.trials[1].provider_failure is not None
    assert report.trials[1].provider_failure.code == "transport"
    assert "供应商原始错误标记" not in report.model_dump_json()
    assert report.summary.passed_trials == 1
    assert report.summary.provider_failed_trials == 1
    assert report.summary.runtime_failed_trials == 1
    assert report.summary.task_failed_trials == 1
    assert report.summary.model_attempts == 14
    assert (report.summary.input_tokens, report.summary.output_tokens) == (140, 28)
    assert report.summary.elapsed_p50_seconds == 2
    assert report.summary.elapsed_p95_seconds == 4
    assert report.summary.cost_completeness == "complete"
    assert report.summary.known_cost_currency == "USD"
    assert report.summary.known_cost_amount == "0.00035"


def test_unknown_failed_usage_keeps_partial_cost_without_inventing_zero() -> None:
    run_ids = (uuid4(), uuid4())
    report = build_coding_eval_campaign_report(
        plan(*run_ids),
        (
            evidence(run_ids[0]),
            evidence(run_ids[1], outcome="provider", usage_complete=False),
        ),
    )

    assert report.trials[1].classification == "provider"
    assert report.trials[1].cost_completeness == "unknown"
    assert report.trials[1].known_cost_amount is None
    assert report.summary.cost_completeness == "partial"
    assert report.summary.known_cost_amount == "0.00015"
    assert report.summary.incomplete_cost_run_ids == (run_ids[1],)


def test_campaign_rejects_missing_or_cross_run_evidence() -> None:
    run_ids = (uuid4(), uuid4())
    current_plan = plan(*run_ids)
    first = evidence(run_ids[0])
    second = evidence(run_ids[1])

    with pytest.raises(KernelError) as incomplete:
        build_coding_eval_campaign_report(current_plan, (first,))
    assert incomplete.value.code == "eval_campaign_incomplete"

    crossed = CompletedCodingEvalTrial(
        state=second.state.model_copy(update={"run_id": run_ids[0]}),
        report=second.report,
        turn=second.turn,
        cost=second.cost,
    )
    with pytest.raises(KernelError) as mismatch:
        build_coding_eval_campaign_report(current_plan, (first, crossed))
    assert mismatch.value.code == "eval_campaign_evidence_invalid"

    wrong_digest = CompletedCodingEvalTrial(
        state=second.state.model_copy(update={"report_sha256": "0" * 64}),
        report=second.report,
        turn=second.turn,
        cost=second.cost,
    )
    with pytest.raises(KernelError) as digest:
        build_coding_eval_campaign_report(current_plan, (first, wrong_digest))
    assert digest.value.code == "eval_campaign_evidence_invalid"


def test_campaign_rejects_cost_from_another_price_snapshot() -> None:
    run_ids = (uuid4(), uuid4())
    first = evidence(run_ids[0])
    second = evidence(run_ids[1])
    other_cost = build_cost_report(
        second.turn,
        tuple(
            bind_price(item, price(version="different-v1"), context())
            for item in second.turn.model_attempts
        ),
    )
    second = CompletedCodingEvalTrial(
        state=second.state,
        report=second.report,
        turn=second.turn,
        cost=other_cost,
    )

    with pytest.raises(KernelError) as error:
        build_coding_eval_campaign_report(plan(*run_ids), (first, second))
    assert error.value.code == "eval_campaign_cost_invalid"


def test_campaign_contract_recomputes_plan_and_summary() -> None:
    run_ids = (uuid4(), uuid4())
    report = build_coding_eval_campaign_report(
        plan(*run_ids), (evidence(run_ids[0]), evidence(run_ids[1]))
    )
    body = json.loads(report.model_dump_json())
    body["summary"]["passed_trials"] = 0
    with pytest.raises(ValidationError):
        CodingEvalCampaignReport.model_validate(body)
    body = json.loads(report.model_dump_json())
    body["plan_fingerprint"] = "0" * 64
    with pytest.raises(ValidationError):
        CodingEvalCampaignReport.model_validate(body)


def test_campaign_plan_rejects_duplicate_runs_and_pricing_scope_drift() -> None:
    run_ids = (uuid4(), uuid4())
    body = plan(*run_ids).model_dump(mode="json")
    body["run_ids"] = [str(run_ids[0]), str(run_ids[0])]
    with pytest.raises(ValidationError):
        CodingEvalCampaignPlan.model_validate(body)

    body = plan(*run_ids).model_dump(mode="json")
    body["billing_context"]["region"] = "other-region"
    with pytest.raises(ValidationError):
        CodingEvalCampaignPlan.model_validate(body)

    body = plan(*run_ids).model_dump(mode="json")
    body["environment"]["model"] = "floating-alias"
    with pytest.raises(ValidationError):
        CodingEvalCampaignPlan.model_validate(body)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("coding-eval-campaign-plan", CodingEvalCampaignPlan),
        ("coding-eval-campaign-report", CodingEvalCampaignReport),
    ],
)
def test_campaign_public_schema_is_frozen(name: str, model: type[ContractModel]) -> None:
    expected = json.loads(Path(f"spec/{name}-v1.schema.json").read_text(encoding="utf-8"))
    assert expected == model.model_json_schema()


def test_campaign_report_round_trip_is_private_atomic_and_bounded(tmp_path: Path) -> None:
    run_ids = (uuid4(), uuid4())
    current_plan = plan(*run_ids)
    report = build_coding_eval_campaign_report(
        current_plan, (evidence(run_ids[0]), evidence(run_ids[1]))
    )
    plan_path = tmp_path / "campaign-plan.json"
    path = tmp_path / "campaign-report.json"

    write_eval_campaign_plan(plan_path, current_plan)
    write_eval_campaign_report(path, report)
    assert read_eval_campaign_plan(plan_path) == current_plan
    assert read_eval_campaign_report(path) == report
    assert plan_path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.tmp"))

    path.chmod(0o644)
    with pytest.raises(KernelError) as permissions:
        read_eval_campaign_report(path)
    assert permissions.value.code == "eval_campaign_report_invalid"

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(KernelError) as link:
        write_eval_campaign_report(path, report)
    assert link.value.code == "eval_campaign_report_path_denied"

    invalid_plan = current_plan.model_copy(update={"run_ids": (run_ids[0], run_ids[0])})
    invalid_plan_path = tmp_path / "invalid-plan.json"
    with pytest.raises(KernelError) as invalid:
        write_eval_campaign_plan(invalid_plan_path, invalid_plan)
    assert invalid.value.code == "eval_campaign_plan_invalid"
    assert not invalid_plan_path.exists()
