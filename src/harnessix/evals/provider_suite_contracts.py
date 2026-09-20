"""受控真实Provider Task Pack Suite的运行与公开证据契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.suite_execution_contracts import (
    CodingEvalSuiteRunConfig,
    SuiteRunReason,
)
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.pricing import Amount, Currency, content_digest

ProviderSuiteRunReason = (
    SuiteRunReason
    | Literal[
        "network_not_enabled",
        "configuration_invalid",
        "dependency_missing",
        "internal_error",
    ]
)


class ProviderSuiteContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CodingEvalProviderSuiteRunConfig(ProviderSuiteContract):
    """由可信宿主提供的真实Provider Suite配置；只引用凭据环境变量。"""

    spec_version: Literal["harnessix.coding-eval-provider-suite-run-config/v1"] = (
        "harnessix.coding-eval-provider-suite-run-config/v1"
    )
    suite: CodingEvalSuiteRunConfig
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    pack_version: int = Field(ge=1, strict=True)
    pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_root: str = Field(min_length=1, max_length=4096)
    git_executable: str = Field(min_length=1, max_length=4096)
    container_engine: str = Field(min_length=1, max_length=4096)
    provider_config: OpenAIChatConfig

    @field_validator("source_root")
    @classmethod
    def canonical_absolute_root(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("真实Provider Suite源码根必须是绝对路径")
        return str(Path(value).resolve(strict=False))

    @field_validator("git_executable", "container_engine")
    @classmethod
    def absolute_executable(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("真实Provider Suite宿主程序必须使用绝对路径")
        return value

    @model_validator(mode="after")
    def fixed_execution_scope(self) -> Self:
        environment = self.suite.plan.environment
        provider = self.provider_config
        if environment.provider != "openai_chat" or environment.model != provider.model:
            raise ValueError("真实Provider Suite环境与Provider配置不一致")
        if environment.isolation != "fixed-container-checks-provider-network":
            raise ValueError("真实Provider Suite隔离声明不受支持")
        capabilities = provider.capabilities
        if not capabilities.tool_calls or capabilities.parallel_tool_calls:
            raise ValueError("真实Provider Suite要求串行工具调用")
        if provider.max_attempts != 1 or provider.retry_delay_seconds != 0:
            raise ValueError("真实Provider Suite禁止Provider自动重试")
        if provider.max_output_tokens > 4096:
            raise ValueError("真实Provider Suite单请求输出上限不能超过4096 Token")
        if len(self.suite.plan.cases) != 10:
            raise ValueError("真实Provider基线必须固定完整十Case范围")
        for campaign in self.suite.campaign_plans:
            price = campaign.price
            context = campaign.billing_context
            if price.model != provider.model:
                raise ValueError("真实Provider Suite价格模型与Provider不一致")
            if any(
                getattr(price, field) != getattr(context, field)
                for field in ("billing_provider", "region", "service_tier", "inference_mode")
            ):
                raise ValueError("真实Provider Suite计费上下文与价格快照不一致")
            if not price.valid_from <= self.suite.plan.created_at < price.valid_until:
                raise ValueError("真实Provider Suite计划时间不在价格快照窗口内")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class CodingEvalProviderSuiteRunReport(ProviderSuiteContract):
    """CLI只输出稳定原因、计数和已知金额，不回显配置或第三方正文。"""

    spec_version: Literal["harnessix.coding-eval-provider-suite-run-report/v1"] = (
        "harnessix.coding-eval-provider-suite-run-report/v1"
    )
    reason: ProviderSuiteRunReason
    suite_id: UUID | None = None
    scheduled_cases: int = Field(default=0, ge=0, le=50)
    completed_cases: int = Field(default=0, ge=0, le=50)
    current_case_id: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$",
    )
    report_published: bool = False
    known_cost_currency: Currency | None = None
    known_cost_amount: Amount | None = None

    @model_validator(mode="after")
    def report_shape(self) -> Self:
        if self.completed_cases > self.scheduled_cases:
            raise ValueError("真实Provider Suite完成Case数超过计划")
        no_identity = {
            "network_not_enabled",
            "configuration_invalid",
            "dependency_missing",
            "internal_error",
        }
        if self.reason in no_identity:
            if self.suite_id is not None or self.scheduled_cases or self.completed_cases:
                raise ValueError("未读取配置的结果不能携带Suite身份")
        elif self.suite_id is None or self.scheduled_cases < 5:
            raise ValueError("真实Provider Suite结果缺少计划身份")
        if self.reason == "completed":
            if not self.report_published or self.completed_cases != self.scheduled_cases:
                raise ValueError("真实Provider Suite完成结果必须发布全部报告")
        elif self.report_published:
            raise ValueError("真实Provider Suite未完成结果不能声称已发布报告")
        has_cost = self.known_cost_currency is not None or self.known_cost_amount is not None
        if has_cost and (self.known_cost_currency is None or self.known_cost_amount is None):
            raise ValueError("真实Provider Suite成本必须同时包含币种和金额")
        return self


class CodingEvalProviderSuiteEvidenceManifest(ProviderSuiteContract):
    """公开目录的低敏索引；不包含Prompt、正文、路径或Secret。"""

    spec_version: Literal["harnessix.provider-suite-evidence/v1"] = (
        "harnessix.provider-suite-evidence/v1"
    )
    suite_id: UUID
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    pack_version: int = Field(ge=1, strict=True)
    pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    harnessix_revision: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    provider: Literal["openai_chat"] = "openai_chat"
    model: str = Field(min_length=1, max_length=256)
    region: str = Field(min_length=1, max_length=128)
    price_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_source_url: str = Field(min_length=1, max_length=2048)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduled_cases: int = Field(ge=5, le=50)
    scheduled_trials: int = Field(ge=10, le=1000)
    passed_trials: int = Field(ge=0, le=1000)
    tests_passed_trials: int = Field(ge=0, le=1000)
    human_intervention_trials: int = Field(ge=0, le=1000)
    model_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_completeness: Literal["complete"] = "complete"
    known_cost_currency: Currency
    known_cost_amount: Amount
    fee_stop_currency: Currency
    fee_stop_amount: Amount
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def aggregate_shape(self) -> Self:
        if not (
            self.passed_trials <= self.scheduled_trials
            and self.tests_passed_trials <= self.scheduled_trials
            and self.human_intervention_trials <= self.scheduled_trials
        ):
            raise ValueError("真实Provider Suite公开计数超过计划")
        if self.known_cost_currency != self.fee_stop_currency:
            raise ValueError("真实Provider Suite成本与停止线币种不一致")
        return self
