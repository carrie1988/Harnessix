"""将已校验Suite投影为脱敏实验轨迹与评分；不读取Session或调用模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import JsonValue

from harnessix.evals.campaign_contracts import CodingEvalCampaignTrial
from harnessix.evals.langfuse_contracts import LangfuseSuiteExport
from harnessix.evals.langfuse_wire import attributes as _attributes
from harnessix.evals.langfuse_wire import identifier as _identifier
from harnessix.evals.langfuse_wire import score_events, trial_span
from harnessix.evals.report import eval_suite_report_sha256
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCasePlan,
    CodingEvalSuiteReport,
    CodingEvalTranscriptEvidence,
    CodingEvalTrialTestEvidence,
)


@dataclass(frozen=True)
class _ExperimentIdentity:
    source_digest: str
    dataset_id: str
    experiment_id: str


def _experiment_attributes(
    report: CodingEvalSuiteReport,
    planned: CodingEvalSuiteCasePlan,
    trial: CodingEvalCampaignTrial,
    identity: _ExperimentIdentity,
    index: int,
    span_id: str,
) -> dict[str, str | int | bool]:
    return {
        "langfuse.observation.type": "agent",
        "langfuse.environment": "evaluation",
        "langfuse.release": report.plan.environment.harnessix_revision,
        "langfuse.session.id": str(trial.run_id),
        "langfuse.experiment.id": identity.experiment_id,
        "langfuse.experiment.name": f"harnessix-{identity.experiment_id[:16]}",
        "langfuse.experiment.dataset.id": identity.dataset_id,
        "langfuse.experiment.item.id": _identifier(
            "harnessix.eval.item/v1", planned.task_fingerprint, str(index)
        ),
        "langfuse.experiment.item.root_observation_id": span_id,
        "langfuse.observation.input": json.dumps({"case_id": planned.case_id}),
        "langfuse.observation.output": json.dumps({"outcome": trial.eval_outcome}),
    }


def _evidence_attributes(
    report: CodingEvalSuiteReport,
    planned: CodingEvalSuiteCasePlan,
    trial: CodingEvalCampaignTrial,
    transcript: CodingEvalTranscriptEvidence,
    tests: CodingEvalTrialTestEvidence,
    identity: _ExperimentIdentity,
) -> dict[str, str | int | bool]:
    evidence: dict[str, str | int | bool] = {
        "suite_id": str(report.plan.suite_id),
        "suite_version": report.plan.suite_version,
        "budget_source": "not_available_in_suite_report",
        "plan_fingerprint": report.plan_fingerprint,
        "source_report_sha256": identity.source_digest,
        "run_id": str(trial.run_id),
        "turn_id": str(trial.turn_id),
        "case_id": planned.case_id,
        "task_kind": planned.task_kind,
        "task_fingerprint": planned.task_fingerprint,
        "classification": trial.classification,
        "turn_status": trial.turn_status,
        "cost_completeness": trial.cost_completeness,
        "input_tokens": trial.input_tokens,
        "output_tokens": trial.output_tokens,
        "model_attempts": trial.model_attempts,
        "test_outcome": tests.outcome,
        "transcript_sha256": transcript.transcript_sha256,
        "scheduled_trials": report.summary.scheduled_trials,
        "provider": report.plan.environment.provider,
        "requested_model": report.plan.environment.model,
        "actual_models": json.dumps(trial.actual_models),
    }
    if trial.known_cost_amount is not None and trial.known_cost_currency is not None:
        evidence["known_cost_amount"] = trial.known_cost_amount
        evidence["known_cost_currency"] = trial.known_cost_currency
    return {f"langfuse.observation.metadata.{key}": value for key, value in evidence.items()}


def _trial_scores(
    trial: CodingEvalCampaignTrial,
    transcript: CodingEvalTranscriptEvidence,
    tests: CodingEvalTrialTestEvidence,
    trace_id: str,
    span_id: str,
) -> list[dict[str, JsonValue]]:
    values: dict[str, float | str] = {
        "task_passed": float(trial.eval_outcome == "passed"),
        "classification": trial.classification,
        "elapsed_seconds": trial.elapsed_seconds,
        "human_interventions": float(transcript.human_intervention_count),
        "cost_completeness": trial.cost_completeness,
    }
    if tests.outcome != "not_applicable":
        values["tests_passed"] = float(tests.outcome == "passed")
    if trial.known_cost_amount is not None:
        values[f"known_cost_{trial.known_cost_currency}"] = float(trial.known_cost_amount)
    return score_events(trace_id, span_id, values, trial.completed_at)


def _trial_span(
    trial: CodingEvalCampaignTrial,
    trace_id: str,
    span_id: str,
    metadata: dict[str, str | int | bool],
) -> dict[str, JsonValue]:
    return trial_span(
        trace_id,
        span_id,
        trial.started_at,
        trial.completed_at,
        trial.eval_outcome == "passed",
        metadata,
    )


def build_langfuse_suite_export(report: CodingEvalSuiteReport) -> LangfuseSuiteExport:
    """从完整报告重算投影；身份及时间由证据决定，重复导出不产生新试次。

    只选择结构化白名单字段，不输出Prompt、Diff、错误正文或仓库URL。
    每试次只有一个摘要Span；聚合用量不伪装为逐调用generation，不推算缺失费用。
    """

    report = CodingEvalSuiteReport.model_validate_json(report.model_dump_json())
    source_digest = eval_suite_report_sha256(report)
    scope = json.dumps(
        [
            (planned.case_id, planned.task_fingerprint, len(case.campaign.trials))
            for planned, case in zip(report.plan.cases, report.cases, strict=True)
        ],
        separators=(",", ":"),
    )
    identity = _ExperimentIdentity(
        source_digest,
        _identifier("harnessix.eval.dataset/v1", scope),
        _identifier("harnessix.eval.experiment/v1", source_digest),
    )
    spans: list[JsonValue] = []
    scores: list[dict[str, JsonValue]] = []
    for planned, case in zip(report.plan.cases, report.cases, strict=True):
        for index, (trial, transcript, tests) in enumerate(
            zip(case.campaign.trials, case.transcripts, case.tests, strict=True)
        ):
            trace_id = _identifier("harnessix.eval.trace/v1", source_digest, str(trial.run_id))[:32]
            span_id = _identifier("harnessix.eval.root/v1", trace_id)[:16]
            metadata = _experiment_attributes(report, planned, trial, identity, index, span_id)
            metadata.update(
                _evidence_attributes(report, planned, trial, transcript, tests, identity)
            )
            spans.append(_trial_span(trial, trace_id, span_id, metadata))
            scores.extend(_trial_scores(trial, transcript, tests, trace_id, span_id))
    return LangfuseSuiteExport(
        suite_id=report.plan.suite_id,
        source_report_sha256=source_digest,
        summary=report.summary,
        resource_spans=(
            {
                "resource": {"attributes": _attributes({"service.name": "harnessix.evals"})},
                "scopeSpans": [{"scope": {"name": "harnessix.evals.export"}, "spans": spans}],
            },
        ),
        scores=tuple(scores),
    )
