"""Coding Eval真实Campaign执行配置、恢复状态与白名单结果契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.pricing import Amount, Currency, amount_units, content_digest

CampaignExecutionStatus = Literal["ready", "running", "stopped", "completed"]
CampaignStopReason = Literal["fee_limit_reached", "cost_unknown"]
CampaignRunReason = Literal[
    "completed",
    "network_not_enabled",
    "configuration_invalid",
    "dependency_missing",
    "fee_limit_reached",
    "cost_unknown",
    "runtime_failed",
    "internal_error",
    "cancelled",
]


class CampaignExecutionContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CodingEvalCampaignRunConfig(CampaignExecutionContract):
    """由可信宿主提供的单Provider Campaign执行配置；只引用凭据环境变量。"""

    spec_version: Literal["harnessix.coding-eval-campaign-run-config/v1"] = (
        "harnessix.coding-eval-campaign-run-config/v1"
    )
    plan: CodingEvalCampaignPlan
    source_root: str = Field(min_length=1, max_length=4096)
    work_root: str = Field(min_length=1, max_length=4096)
    git_executable: str = Field(min_length=1, max_length=4096)
    python_executable: str = Field(min_length=1, max_length=4096)
    provider_config: OpenAIChatConfig
    fee_stop_currency: Currency
    fee_stop_amount: Amount

    @field_validator("source_root", "work_root")
    @classmethod
    def canonical_absolute_path(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("Campaign宿主路径必须是绝对路径")
        return str(Path(value).resolve(strict=False))

    @field_validator("git_executable", "python_executable")
    @classmethod
    def absolute_executable_path(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("Campaign宿主程序必须使用绝对路径")
        return value

    @model_validator(mode="after")
    def fixed_execution_scope(self) -> Self:
        source = Path(self.source_root)
        work = Path(self.work_root)
        if work == source or work.is_relative_to(source):
            raise ValueError("Campaign私有运行目录不能位于被评测源码目录内")
        if self.plan.environment.provider != "openai_chat":
            raise ValueError("当前Campaign执行器只支持OpenAI Chat兼容Provider")
        if self.provider_config.model != self.plan.environment.model:
            raise ValueError("Campaign Provider模型与计划不一致")
        capabilities = self.provider_config.capabilities
        if not capabilities.tool_calls or capabilities.parallel_tool_calls:
            raise ValueError("Campaign要求串行工具调用能力")
        if self.provider_config.max_attempts != 1 or self.provider_config.retry_delay_seconds != 0:
            raise ValueError("Campaign真实基线禁止Provider自动重试")
        if self.fee_stop_currency != self.plan.price.currency:
            raise ValueError("Campaign费用停止线币种与价格快照不一致")
        if amount_units(self.fee_stop_amount) <= 0:
            raise ValueError("Campaign费用停止线必须大于零")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class CodingEvalCampaignExecutionState(CampaignExecutionContract):
    """Campaign进度账本；完成前缀之外的运行不得被视为已结算。"""

    spec_version: Literal["harnessix.coding-eval-campaign-execution-state/v1"] = (
        "harnessix.coding-eval-campaign-execution-state/v1"
    )
    campaign_id: UUID
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CampaignExecutionStatus
    completed_run_ids: tuple[UUID, ...] = Field(max_length=20)
    known_cost_currency: Currency
    known_cost_amount: Amount
    stop_reason: CampaignStopReason | None = None
    report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def state_shape(self) -> Self:
        if len(set(self.completed_run_ids)) != len(self.completed_run_ids):
            raise ValueError("Campaign已完成运行ID必须唯一")
        if self.updated_at < self.started_at:
            raise ValueError("Campaign更新时间早于开始时间")
        if self.status == "ready" and self.completed_run_ids:
            raise ValueError("Campaign ready状态不能已有完成运行")
        if (self.status == "stopped") != (self.stop_reason is not None):
            raise ValueError("Campaign停止状态与原因不一致")
        if (self.status == "completed") != (self.report_sha256 is not None):
            raise ValueError("Campaign完成状态与报告摘要不一致")
        return self


class CodingEvalCampaignRunReport(CampaignExecutionContract):
    """CLI仅输出固定枚举、计数和已知金额，不回显配置、路径或第三方正文。"""

    spec_version: Literal["harnessix.coding-eval-campaign-run-report/v1"] = (
        "harnessix.coding-eval-campaign-run-report/v1"
    )
    reason: CampaignRunReason
    campaign_id: UUID | None = None
    scheduled_trials: int = Field(default=0, ge=0, le=20)
    completed_trials: int = Field(default=0, ge=0, le=20)
    report_published: bool = False
    known_cost_currency: Currency | None = None
    known_cost_amount: Amount | None = None

    @model_validator(mode="after")
    def report_shape(self) -> Self:
        if self.completed_trials > self.scheduled_trials:
            raise ValueError("Campaign完成试验数超过计划")
        if self.reason == "network_not_enabled":
            if self.campaign_id is not None or self.scheduled_trials or self.completed_trials:
                raise ValueError("禁网结果不能声称已读取Campaign")
        elif self.reason not in {"configuration_invalid", "dependency_missing", "internal_error"}:
            if self.campaign_id is None or self.scheduled_trials < 2:
                raise ValueError("Campaign执行结果缺少计划身份")
        if self.reason == "completed":
            if not self.report_published or self.completed_trials != self.scheduled_trials:
                raise ValueError("Campaign完成结果必须已发布全部试验报告")
        elif self.report_published:
            raise ValueError("非完成结果不能声称已发布报告")
        has_cost = self.known_cost_currency is not None or self.known_cost_amount is not None
        if has_cost and (self.known_cost_currency is None or self.known_cost_amount is None):
            raise ValueError("Campaign已知成本必须同时包含币种和金额")
        return self
