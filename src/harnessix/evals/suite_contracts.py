"""多仓库 Coding Eval Suite、Transcript 证据与聚合报告契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from harnessix.agent.models import TurnStatusV18
from harnessix.domain.models import ContractModel
from harnessix.evals.campaign_contracts import (
    CodingEvalCampaignReport,
    CodingEvalCampaignTrial,
    CostCompleteness,
)
from harnessix.evals.contracts import CodingEvalEnvironment, EvalRepository
from harnessix.models.pricing import (
    Amount,
    Currency,
    amount_units,
    content_digest,
    format_amount,
)

EvalTaskKind = Literal["bug_fix", "feature", "refactor", "test", "review"]
EVAL_TASK_KINDS: tuple[EvalTaskKind, ...] = (
    "bug_fix",
    "feature",
    "refactor",
    "test",
    "review",
)
EvalTestOutcome = Literal["passed", "failed", "not_applicable"]


class SuiteContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CodingEvalRate(SuiteContract):
    """以分子、分母和四舍五入基点同时保存可重算比率。"""

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=1)
    basis_points: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def rate_is_recomputable(self) -> Self:
        if self.numerator > self.denominator:
            raise ValueError("Eval比率分子不能超过分母")
        expected = (self.numerator * 10_000 + self.denominator // 2) // self.denominator
        if self.basis_points != expected:
            raise ValueError("Eval比率基点与分子分母不一致")
        return self


def eval_rate(numerator: int, denominator: int) -> CodingEvalRate:
    return CodingEvalRate(
        numerator=numerator,
        denominator=denominator,
        basis_points=(numerator * 10_000 + denominator // 2) // denominator,
    )


class CodingEvalSuiteCasePlan(SuiteContract):
    """Suite冻结的单个任务、仓库和Campaign身份。"""

    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    task_kind: EvalTaskKind
    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    task_version: int = Field(ge=1)
    task_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository: EvalRepository
    campaign_plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodingEvalSuitePlan(SuiteContract):
    """首个模型请求前固定的多任务、多仓库基线计划。"""

    spec_version: Literal["harnessix.coding-eval-suite-plan/v1"] = (
        "harnessix.coding-eval-suite-plan/v1"
    )
    suite_id: UUID
    suite_version: int = Field(ge=1)
    environment: CodingEvalEnvironment
    cases: tuple[CodingEvalSuiteCasePlan, ...] = Field(min_length=5, max_length=50)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def complete_multi_repository_scope(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        tasks = [(case.task_id, case.task_version) for case in self.cases]
        campaigns = [case.campaign_plan_fingerprint for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Suite Case ID必须唯一")
        if len(tasks) != len(set(tasks)):
            raise ValueError("Suite任务ID与版本必须唯一")
        if len(campaigns) != len(set(campaigns)):
            raise ValueError("Suite Campaign计划必须唯一")
        if set(case.task_kind for case in self.cases) != set(EVAL_TASK_KINDS):
            raise ValueError("Suite必须覆盖Bug Fix、Feature、Refactor、Test和Review")
        repositories = {
            (case.repository.origin, case.repository.source_revision) for case in self.cases
        }
        if len(repositories) < 2:
            raise ValueError("Suite必须覆盖至少两个固定仓库Revision")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class CodingEvalTranscriptEvidence(SuiteContract):
    """从持久Turn重算的脱敏Transcript结构和人工干预证据。"""

    spec_version: Literal["harnessix.coding-eval-transcript-evidence/v1"] = (
        "harnessix.coding-eval-transcript-evidence/v1"
    )
    run_id: UUID
    turn_id: UUID
    turn_status: TurnStatusV18
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_items: int = Field(ge=0)
    model_steps: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    approval_requests: int = Field(ge=0)
    automated_approval_decisions: int = Field(ge=0)
    human_approval_interventions: int = Field(ge=0)
    question_requests: int = Field(ge=0)
    question_answers: int = Field(ge=0)
    steering_messages: int = Field(ge=0)
    manual_recovery_results: int = Field(ge=0)

    @model_validator(mode="after")
    def intervention_counts_are_possible(self) -> Self:
        if (
            self.automated_approval_decisions + self.human_approval_interventions
            > self.approval_requests
        ):
            raise ValueError("审批决定数量超过审批请求")
        if self.question_answers > self.question_requests:
            raise ValueError("问题回答数量超过问题请求")
        return self

    @property
    def human_intervention_count(self) -> int:
        return (
            self.human_approval_interventions
            + self.question_requests
            + self.steering_messages
            + self.manual_recovery_results
        )


class CodingEvalTrialTestEvidence(SuiteContract):
    run_id: UUID
    outcome: EvalTestOutcome
    total_checks: int = Field(ge=0, le=64)
    passed_checks: int = Field(ge=0, le=64)

    @model_validator(mode="after")
    def outcome_matches_checks(self) -> Self:
        if self.outcome == "not_applicable":
            if self.total_checks or self.passed_checks:
                raise ValueError("不适用测试不能携带检查计数")
        elif self.total_checks == 0 or self.passed_checks > self.total_checks:
            raise ValueError("测试结论缺少有效检查计数")
        elif (self.outcome == "passed") != (self.passed_checks == self.total_checks):
            raise ValueError("测试结论与检查计数不一致")
        return self


class CodingEvalSuiteCaseReport(SuiteContract):
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    campaign: CodingEvalCampaignReport
    campaign_report_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcripts: tuple[CodingEvalTranscriptEvidence, ...] = Field(min_length=2, max_length=20)
    tests: tuple[CodingEvalTrialTestEvidence, ...] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def evidence_matches_campaign(self) -> Self:
        run_ids = self.campaign.plan.run_ids
        if self.campaign_report_fingerprint != content_digest(self.campaign):
            raise ValueError("Suite Campaign报告指纹不匹配")
        if tuple(item.run_id for item in self.transcripts) != run_ids:
            raise ValueError("Suite Transcript证据顺序与Campaign不一致")
        if tuple(item.run_id for item in self.tests) != run_ids:
            raise ValueError("Suite测试证据顺序与Campaign不一致")
        for trial, transcript in zip(self.campaign.trials, self.transcripts, strict=True):
            if (
                trial.turn_id != transcript.turn_id
                or trial.turn_status != transcript.turn_status
                or trial.model_attempts != transcript.model_attempts
                or trial.input_tokens != transcript.input_tokens
                or trial.output_tokens != transcript.output_tokens
            ):
                raise ValueError("Suite Transcript证据与Campaign试验不一致")
        return self


class CodingEvalSuiteSummary(SuiteContract):
    scheduled_cases: int = Field(ge=5, le=50)
    repositories: int = Field(ge=2, le=50)
    scheduled_trials: int = Field(ge=10, le=1000)
    passed_trials: int = Field(ge=0)
    tests_applicable_trials: int = Field(ge=1)
    tests_passed_trials: int = Field(ge=0)
    human_intervention_trials: int = Field(ge=0)
    task_success_rate: CodingEvalRate
    test_pass_rate: CodingEvalRate
    human_intervention_rate: CodingEvalRate
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
    incomplete_cost_run_ids: tuple[UUID, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def totals_are_consistent(self) -> Self:
        if len(self.incomplete_cost_run_ids) != len(set(self.incomplete_cost_run_ids)):
            raise ValueError("Suite成本不完整Run ID必须唯一")
        if len(self.incomplete_cost_run_ids) > self.scheduled_trials:
            raise ValueError("Suite成本不完整Run数量超过计划试验数")
        if not (
            self.passed_trials <= self.scheduled_trials
            and self.tests_passed_trials <= self.tests_applicable_trials <= self.scheduled_trials
            and self.human_intervention_trials <= self.scheduled_trials
        ):
            raise ValueError("Suite汇总计数超过计划试验数")
        expected_rates = (
            eval_rate(self.passed_trials, self.scheduled_trials),
            eval_rate(self.tests_passed_trials, self.tests_applicable_trials),
            eval_rate(self.human_intervention_trials, self.scheduled_trials),
        )
        if (
            self.task_success_rate,
            self.test_pass_rate,
            self.human_intervention_rate,
        ) != expected_rates:
            raise ValueError("Suite汇总比率与计数不一致")
        has_cost = self.known_cost_currency is not None or self.known_cost_amount is not None
        if self.cost_completeness == "unknown":
            if has_cost or len(self.incomplete_cost_run_ids) != self.scheduled_trials:
                raise ValueError("未知成本不能携带金额且必须覆盖全部试验")
        elif self.cost_completeness == "complete":
            if self.incomplete_cost_run_ids:
                raise ValueError("完整成本不能携带不完整Run")
            if self.known_cost_currency is None or self.known_cost_amount is None:
                raise ValueError("完整成本必须携带已知金额")
        elif self.known_cost_currency is None or self.known_cost_amount is None:
            raise ValueError("完整或部分成本必须携带已知金额")
        elif not self.incomplete_cost_run_ids:
            raise ValueError("部分成本必须携带不完整Run")
        return self


def _nearest_rank(values: tuple[float, ...], numerator: int, denominator: int) -> float:
    ordered = sorted(values)
    index = (len(ordered) * numerator + denominator - 1) // denominator - 1
    return ordered[max(index, 0)]


@dataclass(frozen=True, slots=True)
class _SuiteCostSummary:
    completeness: CostCompleteness
    currency: Currency | None
    amount: Amount | None
    incomplete_run_ids: tuple[UUID, ...]


def _summarize_suite_costs(
    trials: tuple[CodingEvalCampaignTrial, ...],
) -> _SuiteCostSummary:
    known_amounts: list[Amount] = []
    known_currencies: list[Currency] = []
    for trial in trials:
        amount = trial.known_cost_amount
        currency = trial.known_cost_currency
        if (amount is None) != (currency is None):
            raise ValueError("Suite已知成本缺少金额或币种")
        if amount is not None and currency is not None:
            known_amounts.append(amount)
            known_currencies.append(currency)
    currencies = set(known_currencies)
    if len(currencies) > 1:
        raise ValueError("Suite已知成本必须使用同一币种")
    incomplete = tuple(trial.run_id for trial in trials if trial.cost_completeness != "complete")
    completeness: CostCompleteness = (
        "complete" if not incomplete else "partial" if known_amounts else "unknown"
    )
    amount = (
        format_amount(sum((amount_units(value) for value in known_amounts), start=0))
        if known_amounts
        else None
    )
    return _SuiteCostSummary(
        completeness=completeness,
        currency=next(iter(currencies)) if known_amounts else None,
        amount=amount,
        incomplete_run_ids=incomplete,
    )


def _summarize_suite_tests(
    tests: tuple[CodingEvalTrialTestEvidence, ...],
) -> tuple[int, int]:
    applicable = tuple(test for test in tests if test.outcome != "not_applicable")
    if not applicable:
        raise ValueError("Suite至少需要一个适用的测试试验")
    return len(applicable), sum(test.outcome == "passed" for test in applicable)


def summarize_suite_cases(
    plan: CodingEvalSuitePlan,
    cases: tuple[CodingEvalSuiteCaseReport, ...],
) -> CodingEvalSuiteSummary:
    trials = tuple(trial for case in cases for trial in case.campaign.trials)
    tests = tuple(test for case in cases for test in case.tests)
    transcripts = tuple(item for case in cases for item in case.transcripts)
    elapsed = tuple(trial.elapsed_seconds for trial in trials)
    costs = _summarize_suite_costs(trials)
    applicable_tests, passed_tests = _summarize_suite_tests(tests)
    passed_trials = sum(trial.classification == "passed" for trial in trials)
    human = sum(1 for item in transcripts if item.human_intervention_count > 0)
    repositories = {
        (case.repository.origin, case.repository.source_revision) for case in plan.cases
    }
    return CodingEvalSuiteSummary(
        scheduled_cases=len(cases),
        repositories=len(repositories),
        scheduled_trials=len(trials),
        passed_trials=passed_trials,
        tests_applicable_trials=applicable_tests,
        tests_passed_trials=passed_tests,
        human_intervention_trials=human,
        task_success_rate=eval_rate(passed_trials, len(trials)),
        test_pass_rate=eval_rate(passed_tests, applicable_tests),
        human_intervention_rate=eval_rate(human, len(trials)),
        model_attempts=sum(trial.model_attempts for trial in trials),
        input_tokens=sum(trial.input_tokens for trial in trials),
        output_tokens=sum(trial.output_tokens for trial in trials),
        elapsed_min_seconds=min(elapsed),
        elapsed_p50_seconds=_nearest_rank(elapsed, 50, 100),
        elapsed_p95_seconds=_nearest_rank(elapsed, 95, 100),
        elapsed_max_seconds=max(elapsed),
        cost_completeness=costs.completeness,
        known_cost_currency=costs.currency,
        known_cost_amount=costs.amount,
        incomplete_cost_run_ids=costs.incomplete_run_ids,
    )


class CodingEvalSuiteReport(SuiteContract):
    spec_version: Literal["harnessix.coding-eval-suite-report/v1"] = (
        "harnessix.coding-eval-suite-report/v1"
    )
    plan: CodingEvalSuitePlan
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[CodingEvalSuiteCaseReport, ...] = Field(min_length=5, max_length=50)
    summary: CodingEvalSuiteSummary
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def report_is_recomputable(self) -> Self:
        if self.plan_fingerprint != self.plan.fingerprint:
            raise ValueError("Suite计划指纹不匹配")
        if tuple(case.case_id for case in self.cases) != tuple(
            case.case_id for case in self.plan.cases
        ):
            raise ValueError("Suite Case报告顺序与计划不一致")
        for expected, actual in zip(self.plan.cases, self.cases, strict=True):
            campaign = actual.campaign.plan
            if (
                expected.campaign_plan_fingerprint != campaign.fingerprint
                or expected.task_id != campaign.task_id
                or expected.task_version != campaign.task_version
                or expected.task_fingerprint != campaign.task_fingerprint
                or campaign.environment != self.plan.environment
                or campaign.created_at > self.plan.created_at
            ):
                raise ValueError("Suite Case与任务、环境或Campaign计划不一致")
        expected_summary = summarize_suite_cases(self.plan, self.cases)
        if self.summary != expected_summary:
            raise ValueError("Suite汇总与Case证据不一致")
        if self.completed_at != max(case.campaign.completed_at for case in self.cases):
            raise ValueError("Suite完成时间与Campaign证据不一致")
        return self
