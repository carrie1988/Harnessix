"""SWE-bench 真实 Agent 执行的宿主、Provider 与费用边界。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.pricing import (
    Amount,
    BillingContext,
    FlatInputPrice,
    PriceSnapshot,
    amount_units,
    content_digest,
    format_amount,
    rate_units,
)

_MODEL = "qwen3.8-max"
_PUBLIC_MODEL = "harnessix-qwen3.8-max"
_PROVIDER = "openai_chat"
_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_PRICE_URL = "https://help.aliyun.com/zh/model-studio/qwen3-8-max"
_PRICE_VALIDITY = timedelta(hours=24)
_INPUT_PRICE = "12"
_OUTPUT_PRICE = "36"
_CURRENCY: Literal["CNY"] = "CNY"


class SweBenchAgentRunConfig(ContractModel):
    """不含 Secret 值的单次 25 Case 真实执行配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    spec_version: Literal["harnessix.swebench-agent-run-config/v1"] = (
        "harnessix.swebench-agent-run-config/v1"
    )
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_root: str = Field(min_length=1, max_length=4096)
    work_root: str = Field(min_length=1, max_length=4096)
    cache_root: str = Field(min_length=1, max_length=4096)
    git_executable: str = Field(min_length=1, max_length=4096)
    provider_config: OpenAIChatConfig
    price: PriceSnapshot
    billing_context: BillingContext
    fee_stop_amount: Amount
    reserve_unaccounted_requests: Literal[True] = True

    @field_validator("source_root", "work_root", "cache_root")
    @classmethod
    def absolute_directory_path(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("SWE-bench宿主目录必须是绝对路径")
        return str(Path(value).resolve(strict=False))

    @field_validator("git_executable")
    @classmethod
    def absolute_executable_path(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("SWE-bench Git程序必须是绝对路径")
        return value

    @model_validator(mode="after")
    def fixed_execution_scope(self) -> Self:
        source = Path(self.source_root)
        work = Path(self.work_root)
        cache = Path(self.cache_root)
        if any(path == source or path.is_relative_to(source) for path in (work, cache)):
            raise ValueError("SWE-bench私有运行目录不能位于Harnessix源码树内")
        if work == cache or work.is_relative_to(cache) or cache.is_relative_to(work):
            raise ValueError("SWE-bench运行目录与仓库缓存必须相互隔离")
        provider = self.provider_config
        if (
            provider.base_url != _BASE_URL
            or provider.model != _MODEL
            or provider.max_attempts != 1
            or provider.retry_delay_seconds != 0
            or provider.enable_thinking is not False
            or provider.output_token_parameter != "max_tokens"
            or not provider.capabilities.tool_calls
            or provider.capabilities.parallel_tool_calls
        ):
            raise ValueError("SWE-bench Provider不符合固定无重试串行工具边界")
        if (
            self.price.model != provider.model
            or self.price.currency != _CURRENCY
            or self.price.input_price != FlatInputPrice(per_million=_INPUT_PRICE)
            or self.price.output_per_million != _OUTPUT_PRICE
            or self.billing_context.billing_provider != self.price.billing_provider
            or self.billing_context.region != self.price.region
            or self.billing_context.service_tier != self.price.service_tier
            or self.billing_context.inference_mode != self.price.inference_mode
        ):
            raise ValueError("SWE-bench价格或计费上下文与Provider不一致")
        if amount_units(self.fee_stop_amount) <= 0:
            raise ValueError("SWE-bench费用停止线必须大于零")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)

    def require_plan(self, plan: SweBenchPreRegistrationPlan) -> None:
        parameters = {item.name: item.value for item in plan.model_settings.parameters}
        if (
            self.plan_sha256 != plan.plan_sha256
            or plan.model_name != _PUBLIC_MODEL
            or plan.model_settings.provider != _PROVIDER
            or parameters != {"thinking": "disabled"}
            or self.provider_config.timeout_seconds != plan.task_budget.timeout_seconds
        ):
            raise ValueError("SWE-bench运行配置与预注册计划不一致")
        if amount_units(self.worst_case_amount(plan)) > amount_units(self.fee_stop_amount):
            raise ValueError("SWE-bench费用停止线无法覆盖固定最坏情况")

    def worst_case_amount(self, plan: SweBenchPreRegistrationPlan) -> Amount:
        """覆盖全部已结算预算，外加单个未结算请求的最大预留。"""

        known = (
            len(plan.cases)
            * plan.task_budget.max_tokens
            * max(rate_units(_INPUT_PRICE), rate_units(_OUTPUT_PRICE))
        )
        unknown = (
            self.provider_config.max_request_bytes * rate_units(_INPUT_PRICE)
            + self.provider_config.max_output_tokens * rate_units(_OUTPUT_PRICE)
        )
        return format_amount(known + unknown)


def build_swebench_agent_run_config(
    plan: SweBenchPreRegistrationPlan,
    *,
    source_root: Path,
    work_root: Path,
    cache_root: Path,
    git_executable: Path,
    fee_stop_amount: Amount,
    api_key_env: str = "DASHSCOPE_API_KEY",
    observed_at: datetime | None = None,
) -> SweBenchAgentRunConfig:
    """按已求证的北京 PAYG 价格创建可持久化执行绑定。"""

    now = observed_at or datetime.now(UTC)
    config = SweBenchAgentRunConfig(
        plan_sha256=plan.plan_sha256,
        source_root=str(source_root),
        work_root=str(work_root),
        cache_root=str(cache_root),
        git_executable=str(git_executable),
        provider_config=OpenAIChatConfig(
            base_url=_BASE_URL,
            model=_MODEL,
            api_key_env=api_key_env,
            capabilities=ChatCapabilities(tool_calls=True, parallel_tool_calls=False),
            max_output_tokens=4096,
            timeout_seconds=plan.task_budget.timeout_seconds,
            io_timeout_seconds=180,
            max_attempts=1,
            retry_delay_seconds=0,
            max_request_bytes=250_000,
            max_response_bytes=4 * 1024 * 1024,
            output_token_parameter="max_tokens",
            enable_thinking=False,
        ),
        price=PriceSnapshot(
            version=f"aliyun-bailian-{now.date().isoformat()}-qwen3-8-max",
            source_url=_PRICE_URL,
            billing_provider="aliyun-bailian",
            model=_MODEL,
            region="cn-beijing",
            service_tier="payg",
            inference_mode="non-thinking",
            currency=_CURRENCY,
            valid_from=now,
            valid_until=now + _PRICE_VALIDITY,
            input_tokens_min=0,
            input_tokens_max=991_808,
            input_price=FlatInputPrice(per_million=_INPUT_PRICE),
            output_per_million=_OUTPUT_PRICE,
        ),
        billing_context=BillingContext(
            billing_provider="aliyun-bailian",
            region="cn-beijing",
            service_tier="payg",
            inference_mode="non-thinking",
        ),
        fee_stop_amount=fee_stop_amount,
    )
    config.require_plan(plan)
    return config
