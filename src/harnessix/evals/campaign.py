"""从已持久化的独立Eval与模型成本事实生成多试验报告。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.errors import FailureCategory, KernelError
from harnessix.agent.models import TERMINAL_TURNS, Turn, TurnStatusV18
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignPlan,
    CodingEvalCampaignReport,
    CodingEvalCampaignTrial,
    expected_classification,
    summarize_campaign_trials,
)
from harnessix.evals.contracts import CodingEvalReport, CodingEvalRunState
from harnessix.evals.report import eval_report_sha256
from harnessix.models.contracts import ResponseFailed
from harnessix.models.costs import COST_REPORT_ADAPTER, CostReportRecord, build_cost_report


@dataclass(frozen=True, slots=True)
class CompletedCodingEvalTrial:
    """已完成Eval试验及其环境、报告和可选交付记录。"""

    state: CodingEvalRunState
    report: CodingEvalReport
    turn: Turn
    cost: CostReportRecord


def _provider_failure(turn: Turn, *, allowed: bool) -> ResponseFailed | None:
    if not allowed or turn.error is None or turn.error.category is not FailureCategory.PROVIDER:
        return None
    code = turn.error.code.removeprefix("provider_")
    try:
        return ResponseFailed.model_validate({"code": code, "retryable": turn.error.retryable})
    except ValueError:
        return ResponseFailed(code="unknown", retryable=turn.error.retryable)


def _require_evidence(
    plan: CodingEvalCampaignPlan,
    run_id: UUID,
    evidence: CompletedCodingEvalTrial,
) -> CodingEvalCampaignTrial:
    try:
        state = CodingEvalRunState.model_validate_json(
            evidence.state.model_dump_json(), strict=True
        )
        report = CodingEvalReport.model_validate_json(
            evidence.report.model_dump_json(), strict=True
        )
        turn = Turn.model_validate_json(evidence.turn.model_dump_json(), strict=True)
        cost = COST_REPORT_ADAPTER.validate_json(evidence.cost.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_campaign_evidence_invalid", "Campaign单次证据契约无效") from None
    temporal_valid = False
    if turn.completed_at is not None:
        try:
            temporal_valid = (
                state.started_at <= turn.created_at <= turn.completed_at <= report.completed_at
            )
        except TypeError:
            pass
    if (
        state.status != "completed"
        or state.run_id != run_id
        or report.run_id != run_id
        or state.task_id != plan.task_id
        or report.task_id != plan.task_id
        or state.task_version != plan.task_version
        or report.task_version != plan.task_version
        or state.task_fingerprint != plan.task_fingerprint
        or report.task_fingerprint != plan.task_fingerprint
        or state.environment != plan.environment
        or report.environment != plan.environment
        or state.report_sha256 is None
        or state.report_sha256 != eval_report_sha256(report)
        or state.turn_id != turn.turn_id
        or cost.turn_id != turn.turn_id
        or report.started_at != state.started_at
        or report.baseline_observations != state.baseline_observations
        or report.metrics.model_steps != turn.model_steps
        or report.metrics.input_tokens != turn.usage.input_tokens
        or report.metrics.output_tokens != turn.usage.output_tokens
        or report.completed_at < state.started_at
        or turn.completed_at is None
        or turn.status not in TERMINAL_TURNS
        or not temporal_valid
        or not turn.model_attempts
    ):
        raise KernelError("eval_campaign_evidence_invalid", "Campaign单次运行身份或指标不一致")
    bindings = []
    for entry in cost.entries:
        binding = entry.binding
        if (
            binding is None
            or binding.price != plan.price
            or binding.context != plan.billing_context
        ):
            raise KernelError("eval_campaign_cost_invalid", "Campaign成本未绑定固定价格与上下文")
        bindings.append(binding)
    try:
        expected_cost = build_cost_report(turn, tuple(bindings))
    except ValueError:
        raise KernelError("eval_campaign_cost_invalid", "Campaign成本报告无法从Turn重算") from None
    if expected_cost != cost:
        raise KernelError("eval_campaign_cost_invalid", "Campaign成本报告与Turn事实不一致")
    if len(cost.summary.totals) > 1 or (
        cost.summary.totals and cost.summary.totals[0].currency != plan.price.currency
    ):
        raise KernelError("eval_campaign_cost_invalid", "Campaign成本币种与固定价格不一致")
    provider_failure = _provider_failure(turn, allowed=report.outcome != "invalid")
    classification = expected_classification(
        report.outcome,
        report.failure_categories,
        turn.error.category if turn.error else None,
        provider_failure,
    )
    subtotal = cost.summary.totals[0] if cost.summary.totals else None
    return CodingEvalCampaignTrial(
        run_id=run_id,
        eval_report_sha256=state.report_sha256,
        turn_id=turn.turn_id,
        turn_status=TurnStatusV18(turn.status),
        classification=classification,
        eval_outcome=report.outcome,
        failure_categories=report.failure_categories,
        agent_failure_category=turn.error.category if turn.error else None,
        provider_failure=provider_failure,
        actual_models=tuple(
            sorted(
                {
                    attempt.actual_model
                    for attempt in turn.accounted_attempts
                    if attempt.actual_model is not None
                }
            )
        ),
        model_attempts=len(turn.accounted_attempts),
        input_tokens=report.metrics.input_tokens,
        output_tokens=report.metrics.output_tokens,
        elapsed_seconds=report.metrics.elapsed_seconds,
        cost_completeness=cost.summary.completeness,
        known_cost_currency=subtotal.currency if subtotal else None,
        known_cost_amount=subtotal.known_amount if subtotal else None,
        started_at=report.started_at,
        completed_at=report.completed_at,
    )


def build_coding_eval_campaign_report(
    plan: CodingEvalCampaignPlan,
    completed: tuple[CompletedCodingEvalTrial, ...],
) -> CodingEvalCampaignReport:
    """只汇总完整计划；单次账本缺失或身份漂移时拒绝发布部分成功。"""

    try:
        plan = CodingEvalCampaignPlan.model_validate_json(plan.model_dump_json(), strict=True)
    except ValueError:
        raise KernelError("eval_campaign_plan_invalid", "Campaign计划无效") from None
    if len(completed) != len(plan.run_ids):
        raise KernelError("eval_campaign_incomplete", "Campaign尚未具备全部计划试验证据")
    trials = tuple(
        _require_evidence(plan, run_id, evidence)
        for run_id, evidence in zip(plan.run_ids, completed, strict=True)
    )
    completed_at = max(trial.completed_at for trial in trials)
    return CodingEvalCampaignReport(
        plan=plan,
        plan_fingerprint=plan.fingerprint,
        trials=trials,
        summary=summarize_campaign_trials(trials, plan.price.currency),
        completed_at=completed_at,
    )
