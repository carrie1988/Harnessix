"""Coding Eval Suite执行配置、恢复状态和白名单结果契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.suite_contracts import (
    CodingEvalSuiteCaseReport,
    CodingEvalSuitePlan,
)
from harnessix.models.pricing import Amount, Currency, amount_units, content_digest

SuiteExecutionStatus = Literal["ready", "running", "stopped", "completed"]
SuiteStopReason = Literal[
    "cancelled",
    "fee_limit_reached",
    "cost_unknown",
    "evidence_missing",
    "runtime_failed",
]
SuiteCaseRunReason = Literal[
    "completed",
    "cancelled",
    "fee_limit_reached",
    "cost_unknown",
    "evidence_missing",
    "runtime_failed",
]
SuiteRunReason = Literal[
    "completed",
    "cancelled",
    "fee_limit_reached",
    "cost_unknown",
    "evidence_missing",
    "runtime_failed",
]


class SuiteExecutionContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CodingEvalSuiteRunConfig(SuiteExecutionContract):
    """可信宿主提供的Suite执行配置；不保存Provider凭据或动态命令。"""

    spec_version: Literal["harnessix.coding-eval-suite-run-config/v1"] = (
        "harnessix.coding-eval-suite-run-config/v1"
    )
    plan: CodingEvalSuitePlan
    campaign_plans: tuple[CodingEvalCampaignPlan, ...] = Field(min_length=5, max_length=50)
    work_root: str = Field(min_length=1, max_length=4096)
    fee_stop_currency: Currency
    fee_stop_amount: Amount

    @field_validator("work_root")
    @classmethod
    def canonical_absolute_path(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("Suite私有运行目录必须是绝对路径")
        return str(Path(value).resolve(strict=False))

    @model_validator(mode="after")
    def fixed_execution_scope(self) -> Self:
        if len(self.campaign_plans) != len(self.plan.cases):
            raise ValueError("Suite Case与Campaign计划数量不一致")
        run_ids: list[UUID] = []
        for case, campaign in zip(self.plan.cases, self.campaign_plans, strict=True):
            if (
                case.campaign_plan_fingerprint != campaign.fingerprint
                or case.task_id != campaign.task_id
                or case.task_version != campaign.task_version
                or case.task_fingerprint != campaign.task_fingerprint
                or campaign.environment != self.plan.environment
                or campaign.created_at > self.plan.created_at
            ):
                raise ValueError("Suite Case与Campaign计划身份不一致")
            if campaign.price.currency != self.fee_stop_currency:
                raise ValueError("Suite Campaign价格币种与停止线不一致")
            run_ids.extend(campaign.run_ids)
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("Suite全部Campaign的Run ID必须唯一")
        if amount_units(self.fee_stop_amount) <= 0:
            raise ValueError("Suite费用停止线必须大于零")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class CodingEvalSuiteCaseRunResult(SuiteExecutionContract):
    """Case执行适配器只返回稳定原因或完整脱敏证据。"""

    spec_version: Literal["harnessix.coding-eval-suite-case-run-result/v1"] = (
        "harnessix.coding-eval-suite-case-run-result/v1"
    )
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    reason: SuiteCaseRunReason
    report: CodingEvalSuiteCaseReport | None = None

    @model_validator(mode="after")
    def result_shape(self) -> Self:
        if (self.reason == "completed") != (self.report is not None):
            raise ValueError("Suite Case完成原因与报告存在性不一致")
        if self.report is not None and self.report.case_id != self.case_id:
            raise ValueError("Suite Case结果与报告身份不一致")
        return self


class CodingEvalSuiteExecutionState(SuiteExecutionContract):
    """Suite进度账本；只有连续完成前缀可进入聚合。"""

    spec_version: Literal["harnessix.coding-eval-suite-execution-state/v1"] = (
        "harnessix.coding-eval-suite-execution-state/v1"
    )
    suite_id: UUID
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: SuiteExecutionStatus
    completed_case_ids: tuple[str, ...] = Field(max_length=50)
    current_case_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    known_cost_currency: Currency
    known_cost_amount: Amount
    stop_reason: SuiteStopReason | None = None
    report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def state_shape(self) -> Self:
        if len(self.completed_case_ids) != len(set(self.completed_case_ids)):
            raise ValueError("Suite已完成Case ID必须唯一")
        if self.updated_at < self.started_at:
            raise ValueError("Suite更新时间早于开始时间")
        if self.status == "ready" and (self.completed_case_ids or self.current_case_id):
            raise ValueError("Suite ready状态不能已有Case进度")
        if (self.status == "stopped") != (self.stop_reason is not None):
            raise ValueError("Suite停止状态与原因不一致")
        if (self.status == "completed") != (self.report_sha256 is not None):
            raise ValueError("Suite完成状态与报告摘要不一致")
        if self.status == "completed" and self.current_case_id is not None:
            raise ValueError("Suite完成状态不能保留当前Case")
        return self


class CodingEvalSuiteRunReport(SuiteExecutionContract):
    """调用面只暴露身份、计数、停止原因与已知费用。"""

    spec_version: Literal["harnessix.coding-eval-suite-run-report/v1"] = (
        "harnessix.coding-eval-suite-run-report/v1"
    )
    reason: SuiteRunReason
    suite_id: UUID
    scheduled_cases: int = Field(ge=5, le=50)
    completed_cases: int = Field(ge=0, le=50)
    current_case_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    report_published: bool = False
    known_cost_currency: Currency
    known_cost_amount: Amount

    @model_validator(mode="after")
    def report_shape(self) -> Self:
        if self.completed_cases > self.scheduled_cases:
            raise ValueError("Suite完成Case数超过计划")
        if self.reason == "completed":
            if not self.report_published or self.completed_cases != self.scheduled_cases:
                raise ValueError("Suite完成结果必须已发布全部Case报告")
            if self.current_case_id is not None:
                raise ValueError("Suite完成结果不能保留当前Case")
        elif self.report_published:
            raise ValueError("Suite未完成结果不能声称已发布报告")
        return self
