"""Coding Eval多试验计划、单次摘要与聚合报告契约。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from harnessix.agent.errors import FailureCategory
from harnessix.agent.models import TurnStatusV18
from harnessix.agent.usage import ModelIdentifier
from harnessix.domain.models import ContractModel
from harnessix.evals.contracts import CodingEvalEnvironment, EvalFailureCategory, EvalOutcome
from harnessix.models.contracts import ResponseFailed
from harnessix.models.pricing import (
    Amount,
    BillingContext,
    Currency,
    PartitionedInputPrice,
    PriceSnapshot,
    amount_units,
    content_digest,
    format_amount,
)

CampaignClassification = Literal[
    "passed",
    "provider",
    "eval_infrastructure",
    "runtime",
    "task",
    "budget",
]
CAMPAIGN_CLASSIFICATIONS: tuple[CampaignClassification, ...] = (
    "passed",
    "provider",
    "eval_infrastructure",
    "runtime",
    "task",
    "budget",
)
CostCompleteness = Literal["complete", "partial", "unknown"]
_TASK_FAILURES = {"correctness", "regression", "forbidden_edit", "final_answer"}


class CampaignContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CodingEvalCampaignPlan(CampaignContract):
    """在任何真实请求前固定的可比较试验集合与计价上下文。"""

    spec_version: Literal["harnessix.coding-eval-campaign-plan/v1"] = (
        "harnessix.coding-eval-campaign-plan/v1"
    )
    campaign_id: UUID
    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    task_version: int = Field(ge=1)
    task_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: CodingEvalEnvironment
    run_ids: tuple[UUID, ...] = Field(min_length=2, max_length=20)
    price: PriceSnapshot
    billing_context: BillingContext
    created_at: AwareDatetime

    @model_validator(mode="after")
    def fixed_comparable_scope(self) -> Self:
        if len(set(self.run_ids)) != len(self.run_ids) or self.campaign_id in self.run_ids:
            raise ValueError("Campaign运行ID必须唯一")
        if self.environment.model != self.price.model:
            raise ValueError("Campaign必须使用与价格快照一致的固定模型")
        for name in ("billing_provider", "region", "service_tier", "inference_mode"):
            if getattr(self.billing_context, name) != getattr(self.price, name):
                raise ValueError("Campaign计费上下文与价格快照范围不一致")
        expected_ttl = (
            self.price.input_price.cache_write_ttl
            if isinstance(self.price.input_price, PartitionedInputPrice)
            else None
        )
        if self.billing_context.cache_write_ttl != expected_ttl:
            raise ValueError("Campaign缓存计费上下文与价格快照不一致")
        if not self.price.valid_from <= self.created_at < self.price.valid_until:
            raise ValueError("Campaign创建时间不在价格快照有效期内")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


def expected_classification(
    outcome: EvalOutcome,
    failures: tuple[EvalFailureCategory, ...],
    agent_failure_category: FailureCategory | None,
    provider_failure: ResponseFailed | None,
) -> CampaignClassification:
    if outcome == "passed":
        return "passed"
    if "eval_infrastructure" in failures:
        return "eval_infrastructure"
    if agent_failure_category is FailureCategory.PROVIDER:
        return "provider"
    if agent_failure_category is FailureCategory.BUDGET or "budget" in failures:
        return "budget"
    if agent_failure_category is not None:
        return "runtime"
    if _TASK_FAILURES.intersection(failures):
        return "task"
    return "runtime"


class CodingEvalCampaignTrial(CampaignContract):
    run_id: UUID
    eval_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_id: UUID
    turn_status: TurnStatusV18
    classification: CampaignClassification
    eval_outcome: EvalOutcome
    failure_categories: tuple[EvalFailureCategory, ...]
    agent_failure_category: FailureCategory | None = None
    provider_failure: ResponseFailed | None = None
    actual_models: tuple[ModelIdentifier, ...] = Field(max_length=32)
    model_attempts: int = Field(ge=1, le=32_000)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0, le=86_400)
    cost_completeness: CostCompleteness
    known_cost_currency: Currency | None = None
    known_cost_amount: Amount | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def evidence_is_consistent(self) -> Self:
        if list(self.failure_categories) != sorted(set(self.failure_categories)):
            raise ValueError("Campaign失败分类必须唯一并排序")
        if list(self.actual_models) != sorted(set(self.actual_models)):
            raise ValueError("Campaign实际模型必须唯一并排序")
        if self.completed_at < self.started_at:
            raise ValueError("Campaign试验完成时间早于开始时间")
        if self.classification != expected_classification(
            self.eval_outcome,
            self.failure_categories,
            self.agent_failure_category,
            self.provider_failure,
        ):
            raise ValueError("Campaign主失败分类与Eval证据不一致")
        if (self.eval_outcome == "passed") != (not self.failure_categories) or (
            (self.eval_outcome == "invalid") != ("eval_infrastructure" in self.failure_categories)
        ):
            raise ValueError("Campaign Eval结论与失败集合不一致")
        if (self.agent_failure_category is FailureCategory.PROVIDER) != (
            self.provider_failure is not None
        ):
            raise ValueError("Campaign Provider分类与规范失败不一致")
        if self.classification == "passed" and self.agent_failure_category is not None:
            raise ValueError("Campaign通过试验不能携带Agent失败")
        if self.classification == "passed" and self.turn_status != TurnStatusV18.COMPLETED:
            raise ValueError("Campaign通过试验必须由完成Turn产生")
        has_known_cost = self.known_cost_currency is not None or self.known_cost_amount is not None
        if self.cost_completeness == "unknown":
            if has_known_cost:
                raise ValueError("未知成本不能携带已知金额")
        elif self.known_cost_currency is None or self.known_cost_amount is None:
            raise ValueError("完整或部分成本必须携带已知小计")
        return self


class CodingEvalCampaignSummary(CampaignContract):
    scheduled_trials: int = Field(ge=2, le=20)
    passed_trials: int = Field(ge=0)
    provider_failed_trials: int = Field(ge=0)
    eval_infrastructure_failed_trials: int = Field(ge=0)
    runtime_failed_trials: int = Field(ge=0)
    task_failed_trials: int = Field(ge=0)
    budget_failed_trials: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_min_seconds: float = Field(ge=0, le=86_400)
    elapsed_p50_seconds: float = Field(ge=0, le=86_400)
    elapsed_p95_seconds: float = Field(ge=0, le=86_400)
    elapsed_max_seconds: float = Field(ge=0, le=86_400)
    cost_completeness: CostCompleteness
    known_cost_currency: Currency | None = None
    known_cost_amount: Amount | None = None
    incomplete_cost_run_ids: tuple[UUID, ...] = Field(max_length=20)


def _nearest_rank(values: tuple[float, ...], numerator: int, denominator: int) -> float:
    ordered = sorted(values)
    index = (len(ordered) * numerator + denominator - 1) // denominator - 1
    return ordered[max(index, 0)]


def summarize_campaign_trials(
    trials: tuple[CodingEvalCampaignTrial, ...], currency: Currency
) -> CodingEvalCampaignSummary:
    elapsed = tuple(trial.elapsed_seconds for trial in trials)
    known = tuple(trial for trial in trials if trial.known_cost_amount is not None)
    if any(trial.known_cost_currency != currency for trial in known):
        raise ValueError("Campaign已知成本币种与固定价格快照不一致")
    amounts = tuple(
        trial.known_cost_amount for trial in trials if trial.known_cost_amount is not None
    )
    amount = format_amount(sum(amount_units(value) for value in amounts))
    incomplete = tuple(trial.run_id for trial in trials if trial.cost_completeness != "complete")
    completeness: CostCompleteness = (
        "complete" if not incomplete else "partial" if known else "unknown"
    )
    counts = {name: 0 for name in CAMPAIGN_CLASSIFICATIONS}
    for trial in trials:
        counts[trial.classification] += 1
    return CodingEvalCampaignSummary(
        scheduled_trials=len(trials),
        passed_trials=counts["passed"],
        provider_failed_trials=counts["provider"],
        eval_infrastructure_failed_trials=counts["eval_infrastructure"],
        runtime_failed_trials=counts["runtime"],
        task_failed_trials=counts["task"],
        budget_failed_trials=counts["budget"],
        model_attempts=sum(trial.model_attempts for trial in trials),
        input_tokens=sum(trial.input_tokens for trial in trials),
        output_tokens=sum(trial.output_tokens for trial in trials),
        elapsed_min_seconds=min(elapsed),
        elapsed_p50_seconds=_nearest_rank(elapsed, 50, 100),
        elapsed_p95_seconds=_nearest_rank(elapsed, 95, 100),
        elapsed_max_seconds=max(elapsed),
        cost_completeness=completeness,
        known_cost_currency=currency if known else None,
        known_cost_amount=amount if known else None,
        incomplete_cost_run_ids=incomplete,
    )


class CodingEvalCampaignReport(CampaignContract):
    spec_version: Literal["harnessix.coding-eval-campaign-report/v1"] = (
        "harnessix.coding-eval-campaign-report/v1"
    )
    plan: CodingEvalCampaignPlan
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    trials: tuple[CodingEvalCampaignTrial, ...] = Field(min_length=2, max_length=20)
    summary: CodingEvalCampaignSummary
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def report_is_recomputable(self) -> Self:
        if self.plan_fingerprint != self.plan.fingerprint:
            raise ValueError("Campaign计划指纹不匹配")
        if tuple(trial.run_id for trial in self.trials) != self.plan.run_ids:
            raise ValueError("Campaign试验顺序或运行ID与计划不一致")
        if any(
            trial.started_at < self.plan.created_at for trial in self.trials
        ) or self.completed_at != max(trial.completed_at for trial in self.trials):
            raise ValueError("Campaign报告完成时间无效")
        expected = summarize_campaign_trials(self.trials, self.plan.price.currency)
        if self.summary != expected:
            raise ValueError("Campaign汇总与单次证据不一致")
        return self
