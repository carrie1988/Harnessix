"""Harbor外部验收的原生报告合同；不冒充原有14项Coding Eval评分证据。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.evals.campaign_contracts import CampaignClassification
from harnessix.evals.contracts import EvalContract, EvalOutcome
from harnessix.evals.suite_contracts import CodingEvalRate, eval_rate


class HarborEvalTrial(EvalContract):
    """冻结计划中的一个槽位；缺失结果也保留，观测用量不代表完整账单。"""

    slot: int = Field(ge=0, le=999)
    slot_id: UUID = Field(
        description="由Job和冻结槽位派生的关联ID，不表示已执行的Harbor试次或原生Turn。"
    )
    task_name: str = Field(min_length=1, max_length=128)
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    harbor_trial_id: UUID | None = None
    source_result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    thread_id: UUID | None = None
    turn_id: UUID | None = None
    outcome: EvalOutcome
    classification: CampaignClassification
    reason: Literal[
        "verified",
        "task_failed",
        "missing_result",
        "verifier_missing",
        "execution_failed",
        "deadline_exceeded",
        "intervention_required",
        "budget_exhausted",
    ]
    reward: float | None = None
    exception_type: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
    verifier_mode: Literal["separate", "shared", "unknown"]
    agent_name: str = Field(min_length=1, max_length=128)
    agent_version: str | None = Field(default=None, max_length=128)
    native_reason: (
        Literal[
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "protocol_error",
            "deadline_exceeded",
            "intervention_required",
        ]
        | None
    ) = None
    native_turn_status: str | None = Field(default=None, max_length=64)
    native_failure_category: (
        Literal[
            "input",
            "provider",
            "tool",
            "approval",
            "budget",
            "cancelled",
            "interrupted",
            "storage",
            "conflict",
            "internal",
        ]
        | None
    ) = None
    observed_model_steps: int | None = Field(default=None, ge=0)
    protocol_evidence_complete: bool | None = None
    tool_results_succeeded: int | None = Field(default=None, ge=0)
    final_assistant_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_input_tokens: int | None = Field(default=None, ge=0)
    observed_output_tokens: int | None = Field(default=None, ge=0)
    reported_cost_usd: float | None = Field(default=None, ge=0)
    usage_completeness: Literal["unknown"] = "unknown"
    cost_completeness: Literal["unknown"] = "unknown"
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def evidence_is_consistent(self) -> Self:
        if (self.harbor_trial_id is None) != (self.source_result_sha256 is None):
            raise ValueError("Harbor结果身份与摘要必须同时存在")
        if self.reason == "missing_result" and self.harbor_trial_id is not None:
            raise ValueError("缺失试次不能携带结果身份")
        if (self.outcome == "passed") != (self.classification == "passed"):
            raise ValueError("通过结论与分类不一致")
        if (self.outcome == "invalid") != (self.classification == "eval_infrastructure"):
            raise ValueError("invalid只表示外部评测证据不完整或基础设施失败")
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("Harbor试次时间倒置")
        return self


class HarborEvalSummary(EvalContract):
    """覆盖冻结计划全部槽位的可重算汇总；不丢弃异常和缺失结果。"""

    scheduled_trials: int = Field(ge=1, le=1000)
    passed_trials: int = Field(ge=0)
    failed_trials: int = Field(ge=0)
    invalid_trials: int = Field(ge=0)
    pass_rate: CodingEvalRate
    cost_completeness: Literal["unknown"] = "unknown"


def summarize_harbor_trials(trials: tuple[HarborEvalTrial, ...]) -> HarborEvalSummary:
    """按全部计划槽位计算分母，只有外部验收明确通过才计入通过数。"""

    passed = sum(trial.outcome == "passed" for trial in trials)
    return HarborEvalSummary(
        scheduled_trials=len(trials),
        passed_trials=passed,
        failed_trials=sum(trial.outcome == "failed" for trial in trials),
        invalid_trials=sum(trial.outcome == "invalid" for trial in trials),
        pass_rate=eval_rate(passed, len(trials)),
    )


class HarborEvalReport(EvalContract):
    """由Harbor冻结输入锁与结果重算的原生外部报告，不包含Prompt或异常正文。"""

    spec_version: Literal["harnessix.harbor-eval-report/v1"] = "harnessix.harbor-eval-report/v1"
    job_id: UUID
    harbor_version: Literal["0.25.0"] = "0.25.0"
    source_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_job_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reward_key: Literal["reward"] = "reward"
    pass_reward: float = Field(default=1.0, ge=1.0, le=1.0)
    automatic_retries: int = Field(default=0, ge=0, le=0)
    source_timezone: str | None = Field(default=None, max_length=128)
    plan_created_at: AwareDatetime
    completed_at: AwareDatetime
    trials: tuple[HarborEvalTrial, ...] = Field(min_length=1, max_length=1000)
    summary: HarborEvalSummary

    @model_validator(mode="after")
    def report_is_recomputable(self) -> Self:
        if self.completed_at < self.plan_created_at:
            raise ValueError("Harbor运行结束早于输入冻结")
        _validate_identities(self.trials)
        if self.summary != summarize_harbor_trials(self.trials):
            raise ValueError("Harbor汇总与全部试次不一致")
        for trial in self.trials:
            _validate_trial_scope(trial, self)
        return self


def _validate_identities(trials: tuple[HarborEvalTrial, ...]) -> None:
    if tuple(trial.slot for trial in trials) != tuple(range(len(trials))):
        raise ValueError("Harbor报告必须覆盖有序且完整的冻结槽位")
    ids = [trial.harbor_trial_id for trial in trials if trial.harbor_trial_id]
    native_turns = [trial.turn_id for trial in trials if trial.turn_id]
    if len(native_turns) != len(set(native_turns)):
        raise ValueError("同一原生Turn不能重复计入独立试次")
    if len(ids) != len(set(ids)) or len({trial.slot_id for trial in trials}) != len(trials):
        raise ValueError("Harbor试次或槽位身份重复")


def _validate_trial_scope(trial: HarborEvalTrial, report: HarborEvalReport) -> None:
    if trial.outcome == "passed" and (
        trial.reward != report.pass_reward
        or trial.exception_type is not None
        or trial.reason != "verified"
        or (trial.agent_name == "harnessix" and trial.native_reason != "completed")
    ):
        raise ValueError("通过试次缺少明确的验收和执行证据")
    if trial.started_at is not None and trial.started_at < report.plan_created_at:
        raise ValueError("试次开始早于冻结输入锁")
    if trial.completed_at is not None and trial.completed_at > report.completed_at:
        raise ValueError("试次结束晚于Harbor运行结束")
