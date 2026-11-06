"""将原生Harbor报告投影为实验与评分；不把外部奖励伪装成14项原生检查。"""

from __future__ import annotations

import json

from pydantic import JsonValue

from harnessix.evals.harbor_contracts import HarborEvalReport, HarborEvalTrial
from harnessix.evals.langfuse_contracts import LangfuseHarborExport
from harnessix.evals.langfuse_wire import attributes, identifier, score_events, trial_span
from harnessix.models.pricing import content_digest


def _metadata(
    report: HarborEvalReport, trial: HarborEvalTrial, digest: str, dataset_id: str, span_id: str
) -> dict[str, str | int | bool]:
    evidence: dict[str, str | int | bool | None] = {
        "source_kind": "harbor",
        "job_id": str(report.job_id),
        "slot_id": str(trial.slot_id),
        "harbor_trial_id": str(trial.harbor_trial_id) if trial.harbor_trial_id else None,
        "source_report_sha256": digest,
        "source_lock_sha256": report.source_lock_sha256,
        "input_lock_sha256": trial.input_lock_sha256,
        "task_digest": trial.task_digest,
        "slot": trial.slot,
        "classification": trial.classification,
        "reason": trial.reason,
        "agent_name": trial.agent_name,
        "agent_version": trial.agent_version,
        "thread_id": str(trial.thread_id) if trial.thread_id else None,
        "turn_id": str(trial.turn_id) if trial.turn_id else None,
        "native_reason": trial.native_reason,
        "native_turn_status": trial.native_turn_status,
        "native_failure_category": trial.native_failure_category,
        "observed_model_steps": trial.observed_model_steps,
        "protocol_evidence_complete": trial.protocol_evidence_complete,
        "tool_results_succeeded": trial.tool_results_succeeded,
        "final_assistant_sha256": trial.final_assistant_sha256,
        "observed_input_tokens": trial.observed_input_tokens,
        "observed_output_tokens": trial.observed_output_tokens,
        "cost_completeness": trial.cost_completeness,
        "usage_completeness": trial.usage_completeness,
        "verifier_mode": trial.verifier_mode,
        "exception_type": trial.exception_type,
        "time_source": "trial"
        if trial.started_at and trial.completed_at
        else "job_window_missing_trial_time",
        "scheduled_trials": report.summary.scheduled_trials,
        "automatic_retries": report.automatic_retries,
    }
    if trial.reported_cost_usd is not None:
        evidence["harbor_reported_cost_usd"] = str(trial.reported_cost_usd)
    result: dict[str, str | int | bool] = {
        "langfuse.observation.type": "agent",
        "langfuse.environment": "evaluation",
        "langfuse.experiment.id": identifier("harnessix.harbor.experiment/v1", digest),
        "langfuse.experiment.name": f"harnessix-harbor-{digest[:16]}",
        "langfuse.experiment.dataset.id": dataset_id,
        "langfuse.experiment.item.id": identifier(
            "harnessix.harbor.item/v1", trial.task_digest, str(trial.slot)
        ),
        "langfuse.experiment.item.root_observation_id": span_id,
        "langfuse.observation.input": json.dumps({"task_name": trial.task_name}),
        "langfuse.observation.output": json.dumps({"outcome": trial.outcome}),
    }
    result.update(
        {
            f"langfuse.observation.metadata.{key}": value
            for key, value in evidence.items()
            if value is not None
        }
    )
    return result


def build_langfuse_harbor_export(report: HarborEvalReport) -> LangfuseHarborExport:
    """重校验完整原生报告并保留所有槽位；缺失试次只使用明确标记的运行时间窗口。"""
    report = HarborEvalReport.model_validate_json(report.model_dump_json())
    digest = content_digest(report)
    scope = json.dumps(
        [(trial.task_name, trial.task_digest) for trial in report.trials], separators=(",", ":")
    )
    dataset_id = identifier("harnessix.harbor.dataset/v1", scope)
    spans: list[JsonValue] = []
    scores: list[dict[str, JsonValue]] = []
    for trial in report.trials:
        trace_id = identifier("harnessix.harbor.trace/v1", digest, str(trial.slot_id))[:32]
        span_id = identifier("harnessix.eval.root/v1", trace_id)[:16]
        start, end = (
            trial.started_at or report.plan_created_at,
            trial.completed_at or report.completed_at,
        )
        spans.append(
            trial_span(
                trace_id,
                span_id,
                start,
                end,
                trial.outcome == "passed",
                _metadata(report, trial, digest, dataset_id, span_id),
            )
        )
        values: dict[str, float | str] = {
            "task_passed": float(trial.outcome == "passed"),
            "classification": trial.classification,
            "cost_completeness": "unknown",
        }
        if trial.reward is not None:
            values["verifier_reward"] = trial.reward
        if trial.started_at is not None and trial.completed_at is not None:
            values["elapsed_seconds"] = (trial.completed_at - trial.started_at).total_seconds()
        scores.extend(score_events(trace_id, span_id, values, end))
    return LangfuseHarborExport(
        job_id=report.job_id,
        source_report_sha256=digest,
        summary=report.summary,
        resource_spans=(
            {
                "resource": {"attributes": attributes({"service.name": "harnessix.evals"})},
                "scopeSpans": [{"scope": {"name": "harnessix.evals.export"}, "spans": spans}],
            },
        ),
        scores=tuple(scores),
    )
