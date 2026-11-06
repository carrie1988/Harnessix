"""把运行报告投影为实验轨迹与评分；只选择合同字段，不输出提示词、补丁或错误正文。"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import JsonValue

from evalkit.contracts import ArmSpec, DatasetItem, EvalRunReport, Outcome, RunPlan, TrialRecord
from evalkit.contracts import trial_outcome as _trial_outcome
from evalkit.digest import content_digest
from evalkit.langfuse.contracts import LangfuseExport
from evalkit.langfuse.wire import Attributes, attributes, identifier, score_events, span

_METADATA = "langfuse.observation.metadata."


def _experiment_name(plan: RunPlan, arm: ArmSpec, digest: str) -> str:
    prefix = "backfilled/" if plan.provenance == "backfilled" else ""
    return f"{prefix}{plan.run_name}/{arm.arm_id}/{digest.removeprefix('sha256:')[:12]}"


def _experiment_attributes(
    report: EvalRunReport,
    digest: str,
    arm: ArmSpec,
    item: DatasetItem,
    trial: TrialRecord,
    outcome: Outcome,
    span_id: str,
) -> Attributes:
    dataset = report.plan.dataset.digest
    return {
        "langfuse.observation.type": "agent",
        "langfuse.environment": "evaluation",
        "langfuse.release": arm.system_version,
        "langfuse.experiment.id": identifier("evalkit.experiment/v1", digest, arm.arm_id),
        "langfuse.experiment.name": _experiment_name(report.plan, arm, digest),
        "langfuse.experiment.dataset.id": identifier("evalkit.dataset/v1", dataset),
        "langfuse.experiment.item.id": identifier(
            "evalkit.item/v1", dataset, item.item_id, item.item_digest, str(trial.attempt)
        ),
        "langfuse.experiment.item.root_observation_id": span_id,
        "langfuse.observation.input": json.dumps(
            {"item_id": item.item_id, "attempt": trial.attempt}
        ),
        "langfuse.observation.output": json.dumps({"outcome": outcome}),
    }


def _metadata(
    report: EvalRunReport,
    digest: str,
    arm: ArmSpec,
    item: DatasetItem,
    trial: TrialRecord,
    outcome: Outcome,
) -> Attributes:
    plan = report.plan
    scheduled = next(s.scheduled for s in report.summary if s.arm_id == arm.arm_id)
    evidence: dict[str, str | int | bool | None] = {
        "run_name": plan.run_name,
        "plan_digest": report.plan_digest,
        "source_report_digest": digest,
        "provenance": plan.provenance,
        "content_policy": plan.content_policy,
        "primary_grader": plan.primary_grader,
        "arm_id": arm.arm_id,
        "system": arm.system,
        "system_version": arm.system_version,
        "config_digest": arm.config_digest,
        "executor": arm.executor,
        "scheduled_trials": scheduled,
        "item_id": item.item_id,
        "item_digest": item.item_digest,
        "attempt": trial.attempt,
        "slot": trial.slot,
        "status": trial.status,
        "reason": trial.reason,
        "outcome": outcome,
        "time_source": "trial" if trial.started_at and trial.completed_at else "report_window",
        "usage_completeness": trial.usage.completeness,
        "usage_model_calls": trial.usage.model_calls,
        "usage_input_tokens": trial.usage.input_tokens,
        "usage_cached_input_tokens": trial.usage.cached_input_tokens,
        "usage_output_tokens": trial.usage.output_tokens,
        "usage_reasoning_tokens": trial.usage.reasoning_tokens,
        "cost_completeness": trial.cost.completeness,
        "cost_currency": trial.cost.currency,
        "cost_amount": trial.cost.amount,
    }
    evidence.update({f"ref_executor_{k}": v for k, v in sorted(trial.executor_refs.items())})
    evidence.update({f"ref_system_{k}": v for k, v in sorted(trial.system_refs.items())})
    return {_METADATA + key: value for key, value in evidence.items() if value is not None}


def _score_values(trial: TrialRecord, outcome: Outcome) -> dict[str, float | str]:
    values: dict[str, float | str] = {"outcome": outcome}
    if trial.started_at is not None and trial.completed_at is not None:
        values["elapsed_seconds"] = (trial.completed_at - trial.started_at).total_seconds()
    for score in trial.scores:
        name = f"{score.grader_id}/{score.name}"
        values[name] = score.value if isinstance(score.value, str) else float(score.value)
    return values


def _window(report: EvalRunReport, trial: TrialRecord) -> tuple[datetime, datetime]:
    """缺少试次时间时使用报告窗口，并在元数据中标记time_source。"""
    earliest = min(
        (t.started_at for t in report.trials if t.started_at is not None),
        default=report.completed_at,
    )
    start = trial.started_at or min(earliest, trial.completed_at or report.completed_at)
    end = trial.completed_at or max(report.completed_at, start)
    return start, end


def build_export(report: EvalRunReport) -> LangfuseExport:
    """重校验报告后投影；全部槽位都生成Trace，缺失和无效试次不会被丢弃。"""
    report = EvalRunReport.model_validate_json(report.model_dump_json())
    digest = content_digest(report)
    plan = report.plan
    primary = next(g for g in plan.graders if g.grader_id == plan.primary_grader)
    arms = {arm.arm_id: arm for arm in plan.arms}
    items = {item.item_id: item for item in plan.dataset.items}
    spans: list[JsonValue] = []
    scores: list[dict[str, JsonValue]] = []
    for trial in report.trials:
        arm, item = arms[trial.arm_id], items[trial.item_id]
        outcome = _trial_outcome(trial, primary)
        trace_id = identifier("evalkit.trace/v1", digest, str(trial.slot))[:32]
        span_id = identifier("evalkit.root/v1", trace_id)[:16]
        start, end = _window(report, trial)
        metadata = _experiment_attributes(report, digest, arm, item, trial, outcome, span_id)
        metadata.update(_metadata(report, digest, arm, item, trial, outcome))
        spans.append(
            span(trace_id, span_id, "evalkit.trial", start, end, outcome == "passed", metadata)
        )
        scores.extend(score_events(trace_id, span_id, _score_values(trial, outcome), end))
    return LangfuseExport(
        source_report_digest=digest,
        plan_digest=report.plan_digest,
        content_policy=plan.content_policy,
        resource_spans=(
            {
                "resource": {"attributes": attributes({"service.name": "evalkit"})},
                "scopeSpans": [{"scope": {"name": "evalkit.langfuse"}, "spans": spans}],
            },
        ),
        scores=tuple(scores),
    )
