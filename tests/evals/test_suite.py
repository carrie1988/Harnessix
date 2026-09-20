from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ApprovalRequestContent,
    Item,
    ItemStatus,
    TurnStatus,
    Usage,
)
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord, ContractModel
from harnessix.evals.campaign import CompletedCodingEvalTrial
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.contracts import CodingEvalRunState, CodingEvalTask, EvalRepository
from harnessix.evals.grader import grade_coding_eval
from harnessix.evals.report import (
    eval_report_sha256,
    read_eval_suite_plan,
    read_eval_suite_report,
    write_eval_suite_plan,
    write_eval_suite_report,
)
from harnessix.evals.suite import (
    CompletedCodingEvalSuiteCase,
    build_coding_eval_suite_report,
    build_transcript_evidence,
)
from harnessix.evals.suite_contracts import (
    EVAL_TASK_KINDS,
    CodingEvalSuiteCasePlan,
    CodingEvalSuitePlan,
    CodingEvalSuiteReport,
    CodingEvalTranscriptEvidence,
    CodingEvalTrialTestEvidence,
    summarize_suite_cases,
)
from harnessix.models.costs import bind_price, build_cost_report
from tests.evals.test_campaign import campaign_environment
from tests.evals.test_grader import (
    BASELINE_REVISION,
    SHA,
    completed_turn,
    git_evidence,
    observations,
    task,
)
from tests.models.pricing_helpers import NOW, attempt, context, price


def _task(index: int) -> CodingEvalTask:
    base = task()
    repository = EvalRepository(
        name="Harnessix" if index < 3 else "Harnessix-SDK",
        origin=(
            "https://github.com/carrie1988/Harnessix"
            if index < 3
            else "https://github.com/carrie1988/Harnessix-SDK"
        ),
        source_revision=("b" if index < 3 else "d") * 40,
        baseline_tree_sha256=SHA,
    )
    return base.model_copy(
        update={
            "task_id": f"suite-task-{index}",
            "repository": repository,
        }
    )


def _campaign(current_task: CodingEvalTask, run_ids: tuple[UUID, UUID]) -> CodingEvalCampaignPlan:
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


def _approval_item(actor: str) -> Item:
    fingerprint = "e" * 64
    return Item(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=ApprovalRequestContent(
            approval_id=uuid4(),
            call_id=uuid4(),
            request_fingerprint=fingerprint,
            decision=ApprovalRecord(
                outcome=ApprovalOutcome.APPROVED,
                actor=actor,
                reason="测试决定",
                request_fingerprint=fingerprint,
                decided_at=NOW,
            ),
        ),
    )


def _trial(
    current_task: CodingEvalTask,
    run_id: UUID,
    *,
    approval_actor: str | None = None,
    profile_evidence: bool = True,
) -> CompletedCodingEvalTrial:
    model_attempts = tuple(attempt(step=step) for step in range(1, 7))
    turn = completed_turn().model_copy(
        update={
            "model_attempts": model_attempts,
            "usage": Usage(input_tokens=60, output_tokens=12),
            "created_at": NOW,
            "completed_at": NOW + timedelta(seconds=1),
        }
    )
    if approval_actor is not None:
        turn = turn.model_copy(
            update={"items": (*turn.items[:-1], _approval_item(approval_actor), turn.items[-1])}
        )
    baseline, final = observations() if profile_evidence else ((), ())
    report = grade_coding_eval(
        current_task,
        turn,
        run_id=run_id,
        environment=campaign_environment(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
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
        tuple(bind_price(model_attempt, price(), context()) for model_attempt in model_attempts),
    )
    return CompletedCodingEvalTrial(state=state, report=report, turn=turn, cost=cost)


def _suite(
    *, profile_evidence: bool = True
) -> tuple[CodingEvalSuitePlan, tuple[CompletedCodingEvalSuiteCase, ...]]:
    completed: list[CompletedCodingEvalSuiteCase] = []
    cases: list[CodingEvalSuiteCasePlan] = []
    for index, kind in enumerate(EVAL_TASK_KINDS):
        current_task = _task(index)
        run_ids = (uuid4(), uuid4())
        campaign = _campaign(current_task, run_ids)
        trials = (
            _trial(
                current_task,
                run_ids[0],
                approval_actor="human-reviewer" if index == 0 else None,
                profile_evidence=profile_evidence,
            ),
            _trial(current_task, run_ids[1], profile_evidence=profile_evidence),
        )
        case_id = f"case-{index}"
        cases.append(
            CodingEvalSuiteCasePlan(
                case_id=case_id,
                task_kind=kind,
                task_id=current_task.task_id,
                task_version=current_task.task_version,
                task_fingerprint=current_task.fingerprint,
                repository=current_task.repository,
                campaign_plan_fingerprint=campaign.fingerprint,
            )
        )
        completed.append(
            CompletedCodingEvalSuiteCase(
                case_id=case_id,
                task=current_task,
                campaign_plan=campaign,
                trials=trials,
            )
        )
    plan = CodingEvalSuitePlan(
        suite_id=uuid4(),
        suite_version=1,
        environment=campaign_environment(),
        cases=tuple(cases),
        created_at=NOW - timedelta(milliseconds=500),
    )
    return plan, tuple(completed)


def test_suite_requires_five_task_kinds_two_repositories_and_unique_campaigns() -> None:
    plan, _ = _suite()
    assert {case.task_kind for case in plan.cases} == set(EVAL_TASK_KINDS)

    body = plan.model_dump(mode="json")
    body["cases"][4]["task_kind"] = "bug_fix"
    with pytest.raises(ValidationError, match="必须覆盖"):
        CodingEvalSuitePlan.model_validate_json(json.dumps(body), strict=True)

    body = plan.model_dump(mode="json")
    for case in body["cases"]:
        case["repository"] = body["cases"][0]["repository"]
    with pytest.raises(ValidationError, match="至少两个"):
        CodingEvalSuitePlan.model_validate_json(json.dumps(body), strict=True)

    body = plan.model_dump(mode="json")
    body["cases"][1]["campaign_plan_fingerprint"] = body["cases"][0]["campaign_plan_fingerprint"]
    with pytest.raises(ValidationError, match="Campaign计划必须唯一"):
        CodingEvalSuitePlan.model_validate_json(json.dumps(body), strict=True)


def test_suite_aggregates_quality_intervention_usage_cost_and_latency() -> None:
    plan, completed = _suite()
    report = build_coding_eval_suite_report(plan, completed)

    assert report.summary.scheduled_cases == 5
    assert report.summary.repositories == 2
    assert report.summary.scheduled_trials == 10
    assert report.summary.passed_trials == 10
    assert report.summary.task_success_rate.basis_points == 10_000
    assert report.summary.tests_passed_trials == 10
    assert report.summary.test_pass_rate.basis_points == 10_000
    assert report.summary.human_intervention_trials == 1
    assert report.summary.human_intervention_rate.basis_points == 1_000
    assert (report.summary.input_tokens, report.summary.output_tokens) == (600, 120)
    assert report.summary.model_attempts == 60
    assert report.summary.cost_completeness == "complete"
    assert report.summary.known_cost_currency == "USD"
    assert all(case.campaign.plan.created_at <= report.plan.created_at for case in report.cases)
    assert report.cases[0].transcripts[0].human_approval_interventions == 1
    assert "human-reviewer" not in report.model_dump_json()
    assert "修复缺陷" not in report.model_dump_json()


def test_suite_counts_missing_required_profile_evidence_as_failed_tests() -> None:
    plan, completed = _suite(profile_evidence=False)

    report = build_coding_eval_suite_report(plan, completed)

    assert report.summary.scheduled_trials == 10
    assert report.summary.tests_applicable_trials == 10
    assert report.summary.tests_passed_trials == 0
    assert report.summary.test_pass_rate.numerator == 0
    assert report.summary.test_pass_rate.denominator == 10
    assert report.summary.test_pass_rate.basis_points == 0
    assert all(
        evidence.outcome == "failed" and evidence.total_checks > 0 and evidence.passed_checks == 0
        for case in report.cases
        for evidence in case.tests
    )


def test_transcript_evidence_excludes_automatic_approval_from_human_rate() -> None:
    current_task = _task(0)
    human = _trial(current_task, uuid4(), approval_actor="human-reviewer")
    automatic = _trial(current_task, uuid4(), approval_actor="harnessix-eval-runner")

    human_evidence = build_transcript_evidence(human.state.run_id, human.turn)
    automatic_evidence = build_transcript_evidence(automatic.state.run_id, automatic.turn)
    assert human_evidence.human_intervention_count == 1
    assert automatic_evidence.human_intervention_count == 0
    assert automatic_evidence.automated_approval_decisions == 1
    assert human_evidence.transcript_sha256 != "0" * 64


def test_transcript_evidence_rejects_non_terminal_turn() -> None:
    turn = completed_turn().model_copy(update={"status": TurnStatus.CALLING_MODEL})

    with pytest.raises(KernelError) as error:
        build_transcript_evidence(uuid4(), turn)
    assert error.value.code == "eval_transcript_status_invalid"


def test_suite_rejects_missing_cross_task_and_tampered_summary() -> None:
    plan, completed = _suite()
    with pytest.raises(KernelError) as missing:
        build_coding_eval_suite_report(plan, completed[:-1])
    assert missing.value.code == "eval_suite_incomplete"

    crossed = list(completed)
    crossed[1] = CompletedCodingEvalSuiteCase(
        case_id=crossed[1].case_id,
        task=crossed[0].task,
        campaign_plan=crossed[1].campaign_plan,
        trials=crossed[1].trials,
    )
    with pytest.raises(KernelError) as mismatch:
        build_coding_eval_suite_report(plan, tuple(crossed))
    assert mismatch.value.code == "eval_suite_case_mismatch"

    report = build_coding_eval_suite_report(plan, completed)
    body = json.loads(report.model_dump_json())
    body["summary"]["passed_trials"] = 9
    with pytest.raises(ValidationError):
        CodingEvalSuiteReport.model_validate_json(json.dumps(body), strict=True)


def test_suite_rejects_report_without_applicable_test_evidence() -> None:
    plan, completed = _suite()
    report = build_coding_eval_suite_report(plan, completed)
    cases = tuple(
        case.model_copy(
            update={
                "tests": tuple(
                    CodingEvalTrialTestEvidence(
                        run_id=test.run_id,
                        outcome="not_applicable",
                        total_checks=0,
                        passed_checks=0,
                    )
                    for test in case.tests
                )
            }
        )
        for case in report.cases
    )

    with pytest.raises(ValueError, match="至少需要一个适用"):
        summarize_suite_cases(plan, cases)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("coding-eval-suite-plan", CodingEvalSuitePlan),
        ("coding-eval-suite-report", CodingEvalSuiteReport),
        ("coding-eval-transcript-evidence", CodingEvalTranscriptEvidence),
    ],
)
def test_suite_public_schema_is_frozen(name: str, model: type[ContractModel]) -> None:
    expected = json.loads(Path(f"spec/{name}-v1.schema.json").read_text(encoding="utf-8"))
    assert expected == model.model_json_schema()


def test_suite_plan_and_report_round_trip_are_private_atomic_and_bounded(
    tmp_path: Path,
) -> None:
    plan, completed = _suite()
    report = build_coding_eval_suite_report(plan, completed)
    plan_path = tmp_path / "suite-plan.json"
    report_path = tmp_path / "suite-report.json"

    write_eval_suite_plan(plan_path, plan)
    write_eval_suite_report(report_path, report)
    assert read_eval_suite_plan(plan_path) == plan
    assert read_eval_suite_report(report_path) == report
    assert plan_path.stat().st_mode & 0o777 == 0o600
    assert report_path.stat().st_mode & 0o777 == 0o600

    report_path.chmod(0o644)
    with pytest.raises(KernelError) as permissions:
        read_eval_suite_report(report_path)
    assert permissions.value.code == "eval_suite_report_invalid"

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    report_path.unlink()
    report_path.symlink_to(target)
    with pytest.raises(KernelError) as link:
        write_eval_suite_report(report_path, report)
    assert link.value.code == "eval_suite_report_path_denied"
