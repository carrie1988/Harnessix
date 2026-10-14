"""SWE-bench各配置版本共享的模型、价格、宿主和预算约束。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import SWE_BENCH_CASE_COUNT, SweBenchPreRegistrationPlan
from harnessix.evals.swebench.public_test_profile import SweBenchCaseProcessProfile
from harnessix.models.config import OpenAIChatConfig
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
from harnessix.product_config.action_contracts import ProductProcessProfile

_MODEL = "qwen3.8-max"
_PUBLIC_MODEL = "harnessix-qwen3.8-max"
_PROVIDER = "openai_chat"
_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_PRICE_URL = "https://help.aliyun.com/zh/model-studio/qwen3-8-max"
_PRICE_VALIDITY = timedelta(hours=24)
_INPUT_PRICE = "12"
_OUTPUT_PRICE = "36"
_CURRENCY: Literal["CNY"] = "CNY"


class _SweBenchAgentRunSettings(ContractModel):
    """版本共享的固定模型、价格和隔离宿主设置，不含Secret。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

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
    case_process_profiles: tuple[SweBenchCaseProcessProfile, ...] = Field(
        min_length=SWE_BENCH_CASE_COUNT,
        max_length=SWE_BENCH_CASE_COUNT,
    )

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
        output_token_parameter = "max_completion_tokens" if self.thinking_enabled else "max_tokens"
        if (
            provider.base_url != _BASE_URL
            or provider.model != _MODEL
            or provider.max_attempts != 1
            or provider.retry_delay_seconds != 0
            or provider.enable_thinking is not self.thinking_enabled
            or provider.output_token_parameter != output_token_parameter
            or provider.thinking_budget != (1024 if self.thinking_enabled else None)
            or not provider.capabilities.tool_calls
        ):
            raise ValueError("SWE-bench Provider不符合固定无重试工具边界")
        if (
            self.price.model != provider.model
            or self.price.currency != _CURRENCY
            or self.price.inference_mode
            != ("thinking" if self.thinking_enabled else "non-thinking")
            or (
                self.thinking_enabled
                and (self.price.input_tokens_min != 0 or self.price.input_tokens_max != 983_616)
            )
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
        instance_ids = [binding.instance_id for binding in self.case_process_profiles]
        if instance_ids != sorted(instance_ids) or len(set(instance_ids)) != len(instance_ids):
            raise ValueError("SWE-bench Case Profile映射必须按instance_id排序且唯一")
        engines = {binding.profile.container_engine for binding in self.case_process_profiles}
        if len(engines) != 1:
            raise ValueError("SWE-bench Case Profile必须绑定同一个Container Engine")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)

    @property
    def isolate_provider_failures(self) -> bool:
        return False

    @property
    def budget_pressure_compaction(self) -> bool:
        return False

    @property
    def thinking_enabled(self) -> bool:
        return False

    @property
    def planning_enabled(self) -> bool:
        return False

    @property
    def grep_snapshots_enabled(self) -> bool:
        return False

    @property
    def closing_phase_enabled(self) -> bool:
        return False

    @property
    def regex_search_enabled(self) -> bool:
        return False

    @property
    def optional_pre_edit_profile_enabled(self) -> bool:
        return False

    @property
    def workspace_retirement_enabled(self) -> bool:
        return False

    @property
    def output_token_limit_margin(self) -> int:
        return 0

    def require_plan(self, plan: SweBenchPreRegistrationPlan) -> None:
        parameters = {item.name: item.value for item in plan.model_settings.parameters}
        expected_parameters: dict[str, str | int | bool] = {"thinking": "disabled"}
        if self.thinking_enabled:
            expected_parameters = {"thinking": "enabled", "thinking_budget": 1024}
        expected_cases = tuple(sorted((case.instance_id, case.repo) for case in plan.cases))
        if (
            self.plan_sha256 != plan.plan_sha256
            or plan.model_name != _PUBLIC_MODEL
            or plan.model_settings.provider != _PROVIDER
            or parameters
            not in (
                expected_parameters,
                {**expected_parameters, "parallel_tool_calls": True},
            )
            or self.provider_config.capabilities.parallel_tool_calls
            is not parameters.get("parallel_tool_calls", False)
            or self.provider_config.timeout_seconds != plan.task_budget.timeout_seconds
            or tuple((binding.instance_id, binding.repo) for binding in self.case_process_profiles)
            != expected_cases
        ):
            raise ValueError("SWE-bench运行配置与预注册计划不一致")
        minimum = self.worst_case_amount(plan)
        if self.isolate_provider_failures:
            minimum = format_amount(amount_units(minimum) // len(plan.cases))
        if amount_units(minimum) > amount_units(self.fee_stop_amount):
            raise ValueError("SWE-bench费用停止线无法覆盖固定最坏情况")

    def process_profile_for(self, instance_id: str) -> ProductProcessProfile:
        """返回配置中该 Case 唯一冻结的完整 Process Profile。"""

        matches = tuple(
            binding.profile
            for binding in self.case_process_profiles
            if binding.instance_id == instance_id
        )
        if len(matches) != 1:
            raise ValueError("SWE-bench Case缺少唯一冻结的Process Profile")
        return matches[0]

    def worst_case_amount(self, plan: SweBenchPreRegistrationPlan) -> Amount:
        """隔离版本逐Case预留；v5/v6包含API容差，不放宽任务硬预算。"""

        known = (
            len(plan.cases)
            * (plan.task_budget.max_tokens + self.output_token_limit_margin)
            * max(rate_units(_INPUT_PRICE), rate_units(_OUTPUT_PRICE))
        )
        unknown = self.provider_config.max_request_bytes * rate_units(_INPUT_PRICE) + (
            self.provider_config.max_output_tokens + self.output_token_limit_margin
        ) * rate_units(_OUTPUT_PRICE)
        return format_amount(
            known + unknown * (len(plan.cases) if self.isolate_provider_failures else 1)
        )
