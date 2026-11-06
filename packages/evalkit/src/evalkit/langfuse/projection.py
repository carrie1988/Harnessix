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
from evalkit.steps import StepTrace

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


def _usage_details(step_usage: object) -> str | None:
    fields = {
        "input": getattr(step_usage, "input_tokens", None),
        "output": getattr(step_usage, "output_tokens", None),
        "input_cached": getattr(step_usage, "cached_input_tokens", None),
        "output_reasoning": getattr(step_usage, "reasoning_tokens", None),
    }
    details = {key: value for key, value in fields.items() if value is not None}
    return json.dumps(details, sort_keys=True) if details else None


def _step_spans(
    trace_id: str, root_span_id: str, trace: StepTrace, full_content: bool
) -> list[JsonValue]:
    """系统适配器提供的步骤投影为根Observation之下的子Observation；正文只在完整内容级别导出。"""
    steps = (trace if full_content else trace.without_content()).steps
    ids = {s.step_id: identifier("evalkit.step/v1", trace_id, s.step_id)[:16] for s in steps}
    result: list[JsonValue] = []
    for step in steps:
        values: Attributes = {
            "langfuse.observation.type": step.kind,
            "langfuse.environment": "evaluation",
            _METADATA + "step_id": step.step_id,
            _METADATA + "step_status": step.status,
        }
        values.update({f"{_METADATA}ref_step_{k}": v for k, v in sorted(step.metadata.items())})
        if step.model is not None:
            values["langfuse.observation.model.name"] = step.model
        usage = _usage_details(step.usage) if step.usage is not None else None
        if usage is not None:
            values["langfuse.observation.usage_details"] = usage
        if step.input_text is not None:
            values["langfuse.observation.input"] = step.input_text
        if step.output_text is not None:
            values["langfuse.observation.output"] = step.output_text
        parent = ids[step.parent_step_id] if step.parent_step_id else root_span_id
        result.append(
            span(
                trace_id,
                ids[step.step_id],
                step.name,
                step.started_at,
                step.completed_at,
                step.status == "ok",
                values,
                parent_span_id=parent,
            )
        )
    return result


def _bind_steps(report: EvalRunReport, step_traces: tuple[StepTrace, ...]) -> dict[int, StepTrace]:
    by_slot: dict[int, StepTrace] = {}
    for trace in step_traces:
        if trace.plan_digest != report.plan_digest or not 0 <= trace.slot < len(report.trials):
            raise ValueError("步骤记录不属于该报告的计划槽位")
        if trace.slot in by_slot or report.trials[trace.slot].status == "missing":
            raise ValueError("步骤记录重复或指向缺失试次")
        by_slot[trace.slot] = trace
    return by_slot


def build_export(report: EvalRunReport, step_traces: tuple[StepTrace, ...] = ()) -> LangfuseExport:
    """重校验报告后投影；全部槽位都生成Trace，缺失和无效试次不会被丢弃。"""
    report = EvalRunReport.model_validate_json(report.model_dump_json())
    steps_by_slot = _bind_steps(report, step_traces)
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
        if trial.slot in steps_by_slot:
            full = plan.content_policy == "full"
            spans.extend(_step_spans(trace_id, span_id, steps_by_slot[trial.slot], full))
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
