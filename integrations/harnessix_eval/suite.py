"""把已校验的Harnessix Suite报告回填为evalkit运行报告。

Suite计划由Harnessix自己冻结，evalkit计划是事后从它推出的，因此标记为backfilled，
created_at取源计划时间，source_refs记录源报告摘要。结果必须与源报告逐试次一致。
"""

from __future__ import annotations

import hashlib

from evalkit.contracts import (
    ArmSpec,
    Cost,
    DatasetItem,
    DatasetManifest,
    EvalRunReport,
    GraderSpec,
    RunPlan,
    Score,
    TrialRecord,
    TrialStatus,
    Usage,
    summarize,
    trial_outcome,
)

from harnessix.evals.campaign_contracts import CodingEvalCampaignTrial
from harnessix.evals.report import eval_suite_report_sha256
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCaseReport,
    CodingEvalSuiteReport,
    CodingEvalTranscriptEvidence,
    CodingEvalTrialTestEvidence,
)

SUITE_GRADER = GraderSpec(
    grader_id="harnessix-suite", version="coding-eval-suite/v1", pass_score="task_passed"
)
ARM_ID = "harnessix"
_STATUS: dict[str, tuple[TrialStatus, str]] = {
    "passed": ("completed", "completed"),
    "task": ("completed", "completed"),
    "budget": ("system_error", "budget_exhausted"),
    "provider": ("system_error", "provider_failed"),
    "runtime": ("system_error", "runtime_failed"),
    "eval_infrastructure": ("infra_error", "eval_infrastructure"),
}


def _short(value: str) -> str:
    return value if len(value) <= 256 else "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _plan(report: CodingEvalSuiteReport, attempts: int) -> RunPlan:
    plan = report.plan
    return RunPlan(
        run_name=f"harnessix-suite-{plan.suite_id}",
        dataset=DatasetManifest(
            name="harnessix-coding-suite",
            version=f"v{plan.suite_version}",
            items=tuple(
                DatasetItem(
                    item_id=case.case_id,
                    item_digest="sha256:" + case.task_fingerprint,
                    labels=(f"task_kind:{case.task_kind}",),
                )
                for case in plan.cases
            ),
        ),
        arms=(
            ArmSpec(
                arm_id=ARM_ID,
                system="harnessix",
                system_version=plan.environment.harnessix_revision,
                config_digest="sha256:" + report.plan_fingerprint,
                executor="import",
            ),
        ),
        attempts_per_item=attempts,
        graders=(SUITE_GRADER,),
        primary_grader=SUITE_GRADER.grader_id,
        provenance="backfilled",
        source_refs={
            "suite_id": str(plan.suite_id),
            "suite_report_sha256": eval_suite_report_sha256(report),
            "suite_plan_fingerprint": report.plan_fingerprint,
        },
        created_at=plan.created_at,
    )


def _trial(
    slot: int,
    attempt: int,
    case: CodingEvalSuiteCaseReport,
    trial: CodingEvalCampaignTrial,
    transcript: CodingEvalTranscriptEvidence,
    tests: CodingEvalTrialTestEvidence,
    report: CodingEvalSuiteReport,
) -> TrialRecord:
    status, reason = _STATUS[trial.classification]
    scores: list[Score] = []
    if status != "infra_error":
        scores.append(
            Score(
                name="task_passed",
                value=trial.eval_outcome == "passed",
                grader_id=SUITE_GRADER.grader_id,
            )
        )
        if tests.outcome != "not_applicable":
            scores.append(
                Score(
                    name="tests_passed",
                    value=tests.outcome == "passed",
                    grader_id=SUITE_GRADER.grader_id,
                )
            )
        scores.append(
            Score(
                name="human_interventions",
                value=float(transcript.human_intervention_count),
                grader_id=SUITE_GRADER.grader_id,
            )
        )
    environment = report.plan.environment
    return TrialRecord(
        slot=slot,
        arm_id=ARM_ID,
        item_id=case.case_id,
        attempt=attempt,
        status=status,
        reason=reason,
        scores=tuple(scores),
        # Suite报告记录了每个试次的累计用量，但没有声明其完整性。
        usage=Usage(
            completeness="unknown",
            model_calls=trial.model_attempts,
            input_tokens=trial.input_tokens,
            output_tokens=trial.output_tokens,
        ),
        cost=Cost(
            completeness=trial.cost_completeness,
            currency=trial.known_cost_currency,
            amount=trial.known_cost_amount,
        ),
        started_at=trial.started_at,
        completed_at=trial.completed_at,
        system_refs={
            "run_id": str(trial.run_id),
            "turn_id": str(trial.turn_id),
            "turn_status": trial.turn_status,
            "classification": trial.classification,
            "transcript_sha256": transcript.transcript_sha256,
            "test_outcome": tests.outcome,
            "provider": environment.provider,
            "requested_model": _short(environment.model),
            "actual_models": _short(",".join(trial.actual_models))
            if trial.actual_models
            else "none",
        },
    )


def suite_run_report(report: CodingEvalSuiteReport) -> EvalRunReport:
    """逐试次转换并核对：evalkit判定的结果必须与Suite报告原结论完全一致。"""
    report = CodingEvalSuiteReport.model_validate_json(report.model_dump_json())
    counts = {len(case.campaign.trials) for case in report.cases}
    if len(counts) != 1:
        raise ValueError("各Case的试次数不同，无法表示为统一的重复次数")
    plan = _plan(report, counts.pop())
    trials = []
    for planned, case in zip(report.plan.cases, report.cases, strict=True):
        if planned.case_id != case.case_id:
            raise ValueError("Suite计划与报告的Case顺序不一致")
        for attempt, evidence in enumerate(
            zip(case.campaign.trials, case.transcripts, case.tests, strict=True)
        ):
            record = _trial(len(trials), attempt, case, *evidence, report)
            if trial_outcome(record, SUITE_GRADER) != evidence[0].eval_outcome:
                raise ValueError("回填结果与Suite报告原结论不一致")
            trials.append(record)
    ordered = tuple(trials)
    return EvalRunReport(
        plan=plan,
        plan_digest=plan.digest,
        trials=ordered,
        summary=summarize(plan, ordered),
        completed_at=report.completed_at,
    )
