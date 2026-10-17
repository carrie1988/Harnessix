"""SWE-bench各配置版本共享的模型、价格、宿主和预算约束。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal, NamedTuple, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import (
    SWE_BENCH_CASE_COUNT,
    SweBenchPreRegistrationPlan,
    SweBenchTaskBudget,
)
from harnessix.evals.swebench.public_test_profile import SweBenchCaseProcessProfile
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.pricing import (
    Amount,
    BillingContext,
    FlatInputPrice,
    PartitionedInputPrice,
    PriceSnapshot,
    amount_units,
    content_digest,
    format_amount,
    max_input_rate,
    rate_units,
)
from harnessix.product_config.action_contracts import ProductProcessProfile

_PROVIDER = "openai_chat"
_PRICE_VALIDITY = timedelta(hours=24)
# 原生思考的推理Token计入max_tokens，输出上限放宽一倍以免推理挤占回答而截断。
_NATIVE_THINKING_OUTPUT = 8192
_CURRENCY: Literal["CNY"] = "CNY"
_BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class CatalogModel(NamedTuple):
    """非思考单价：只收录已按官方页面求证的模型，单价只在input_tokens_max内有效。"""

    public_name: str
    price_url: str
    price_slug: str
    input_price: str
    output_price: str
    input_tokens_max: int
    # 前缀缓存的读取/创建单价；None表示未求证，不得开启缓存计价。
    cache_read_price: str | None = None
    cache_creation_price: str | None = None
    base_url: str = _BAILIAN_URL
    billing_provider: str = "aliyun-bailian"
    region: str = "cn-beijing"
    # explicit_tail需在请求中打缓存标记；automatic由服务端自动缓存前缀，请求不变。
    prompt_cache_mode: Literal["explicit_tail", "automatic"] = "explicit_tail"
    thinking_format: Literal["thinking_type"] | None = None


SweBenchModel = Literal[
    "qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash", "deepseek-flash", "deepseek-v4-pro"
]
MODEL_CATALOG: dict[str, CatalogModel] = {
    "qwen3.8-max": CatalogModel(
        "harnessix-qwen3.8-max",
        "https://help.aliyun.com/zh/model-studio/qwen3-8-max",
        "qwen3-8-max",
        "12",
        "36",
        991_808,
        # 官方：输入缓存命中1.5、显式缓存命中1、显式创建15；读取取两者较高者以保守计费。
        "1.5",
        "15",
    ),
    # 输入超过256K进入更高阶梯；单价只覆盖首阶梯，超出即无法估价并失败关闭。
    "qwen3.7-plus": CatalogModel(
        "harnessix-qwen3.7-plus",
        "https://help.aliyun.com/zh/model-studio/qwen3-7-plus",
        "qwen3-7-plus",
        "2",
        "8",
        256_000,
    ),
    "qwen3.8-flash": CatalogModel(
        "harnessix-qwen3.8-flash",
        "https://help.aliyun.com/zh/model-studio/qwen3-8-flash",
        "qwen3-8-flash",
        "0.8",
        "2.7",
        991_808,
    ),
    # DeepSeek-V4.1-Flash：按高峰时段单价（空闲时段为一半）保守计价；自动前缀缓存无创建附加费，
    # 创建按未命中单价。上下文1M。
    "deepseek-flash": CatalogModel(
        "harnessix-deepseek-flash",
        "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        "deepseek-flash-peak",
        "2",
        "8",
        1_048_576,
        "0.04",
        "2",
        base_url="https://api.deepseek.com",
        billing_provider="deepseek",
        region="cn",
        prompt_cache_mode="automatic",
        thinking_format="thinking_type",
    ),
    # DeepSeek-V4-Pro：同一价格页高峰单价（未命中9、命中0.3、输出27元/百万Token），协议同Flash。
    "deepseek-v4-pro": CatalogModel(
        "harnessix-deepseek-v4-pro",
        "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        "deepseek-v4-pro-peak",
        "9.0",
        "27.0",
        1_048_576,
        "0.30",
        "9.0",
        base_url="https://api.deepseek.com",
        billing_provider="deepseek",
        region="cn",
        prompt_cache_mode="automatic",
        thinking_format="thinking_type",
    ),
}


def catalog_input_price(
    model: CatalogModel, *, prompt_cache: bool
) -> FlatInputPrice | PartitionedInputPrice:
    """目录单价构成的输入价格：开启前缀缓存时按命中/创建/未缓存分区计价。"""
    if not prompt_cache:
        return FlatInputPrice(per_million=model.input_price)
    if model.cache_read_price is None or model.cache_creation_price is None:
        raise ValueError("SWE-bench模型缺少已求证的缓存单价")
    return PartitionedInputPrice(
        uncached_per_million=model.input_price,
        cache_read_per_million=model.cache_read_price,
        cache_creation_per_million=model.cache_creation_price,
        cache_write_ttl=None,
    )


def _wire_prompt_cache(
    model: CatalogModel, *, prompt_cache: bool, stable_tail: bool = False
) -> Literal["explicit_tail", "stable_tail"] | None:
    """显式缓存模型用explicit_tail；自动缓存模型不打标记，冻结版本开启时用stable_tail保持前缀不变。"""
    if not prompt_cache:
        return None
    if model.prompt_cache_mode == "explicit_tail":
        return "explicit_tail"
    return "stable_tail" if stable_tail else None


# 思考诊断只对已求证思考价格的模型开放。
_THINKING_MODEL = "qwen3.8-max"


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
        model = MODEL_CATALOG.get(provider.model)
        if model is None or (self.thinking_enabled and provider.model != _THINKING_MODEL):
            raise ValueError("SWE-bench模型不在已求证价格目录内")
        output_token_parameter = "max_completion_tokens" if self.thinking_enabled else "max_tokens"
        model_thinking = self.thinking_enabled or self.native_thinking
        if self.native_thinking and (
            model.thinking_format is None or provider.max_output_tokens != _NATIVE_THINKING_OUTPUT
        ):
            raise ValueError("SWE-bench原生思考只用于已求证thinking_type格式的模型与固定输出上限")
        if (
            provider.base_url != model.base_url
            or provider.thinking_format != model.thinking_format
            or provider.connect_retries != self.connect_retries
            or (provider.proxy_url is not None and not self.provider_proxy_allowed)
            or provider.repair_tool_arguments is not self.repair_tool_arguments
            or provider.max_attempts != 1
            or provider.retry_delay_seconds != 0
            or provider.enable_thinking is not model_thinking
            or provider.output_token_parameter != output_token_parameter
            or provider.thinking_budget != (1024 if self.thinking_enabled else None)
            or not provider.capabilities.tool_calls
            or provider.prompt_cache
            != _wire_prompt_cache(
                model, prompt_cache=self.prompt_cache, stable_tail=self.stable_cache_tail
            )
        ):
            raise ValueError("SWE-bench Provider不符合固定无重试工具边界")
        if (
            self.price.model != provider.model
            or self.price.currency != _CURRENCY
            or self.price.billing_provider != model.billing_provider
            or self.price.region != model.region
            or self.price.inference_mode != ("thinking" if model_thinking else "non-thinking")
            or (
                self.thinking_enabled
                and (self.price.input_tokens_min != 0 or self.price.input_tokens_max != 983_616)
            )
            or (
                not self.thinking_enabled
                and (
                    self.price.input_tokens_min != 0
                    or self.price.input_tokens_max != model.input_tokens_max
                )
            )
            or self.price.input_price != self.expected_input_price
            or self.price.output_per_million != model.output_price
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
    def model(self) -> CatalogModel:
        return MODEL_CATALOG[self.provider_config.model]

    @property
    def prompt_cache(self) -> bool:
        return False

    @property
    def expected_input_price(self) -> FlatInputPrice | PartitionedInputPrice:
        return catalog_input_price(self.model, prompt_cache=self.prompt_cache)

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
    def definition_views_enabled(self) -> bool:
        return False

    @property
    def working_set_result_groups(self) -> int | None:
        return None

    @property
    def compact_model_surface(self) -> bool:
        return False

    @property
    def convergence_guidance(self) -> bool:
        return False

    @property
    def patch_deadline(self) -> int | None:
        return None

    @property
    def budget_capped_truncation(self) -> bool:
        return False

    @property
    def normalize_stringified_arguments(self) -> bool:
        return False

    @property
    def stable_tool_catalog(self) -> bool:
        return False

    @property
    def reopen_after_clean_review(self) -> bool:
        return False

    @property
    def scratch_reproduction(self) -> bool:
        return False

    @property
    def isolate_protocol_failures(self) -> bool:
        return False

    @property
    def state_tool_call_cap(self) -> bool:
        return False

    @property
    def explain_tool_gates(self) -> bool:
        return False

    @property
    def bounded_output_reserve(self) -> bool:
        return False

    @property
    def connect_retries(self) -> int:
        return 0

    @property
    def reject_forced_choice_violations(self) -> bool:
        return False

    @property
    def repair_tool_arguments(self) -> bool:
        return False

    @property
    def stable_cache_tail(self) -> bool:
        return False

    @property
    def test_timeouts_are_failures(self) -> bool:
        return False

    @property
    def keep_reads_after_failed_patch(self) -> bool:
        return False

    @property
    def truncation_is_quality_failure(self) -> bool:
        return False

    @property
    def native_thinking(self) -> bool:
        return False

    @property
    def self_verification(self) -> bool:
        return False

    @property
    def bounded_repair_cycles(self) -> bool:
        return False

    @property
    def explain_clean_review(self) -> bool:
        return False

    @property
    def provider_proxy_allowed(self) -> bool:
        return False

    def known_case_reserve_units(
        self, budget: SweBenchTaskBudget, *, input_rate: int, output_rate: int
    ) -> int:
        """单Case已知用量的最坏费用：Token硬预算内输入与输出合计不超过上限。

        有界版本另用“每请求输出不超过provider上限、请求数（含摘要与重试）不超过max_steps”
        约束输出部分，其余Token只能是输入；默认版本把全部Token按较高费率计价。
        """
        total = budget.max_tokens + self.output_token_limit_margin
        if not self.bounded_output_reserve:
            return total * max(input_rate, output_rate)
        output = min(
            total,
            budget.max_steps
            * (self.provider_config.max_output_tokens + self.output_token_limit_margin),
        )
        return output * max(input_rate, output_rate) + (total - output) * input_rate

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
        if self.native_thinking:
            expected_parameters = {"thinking": "enabled"}
        expected_cases = tuple(sorted((case.instance_id, case.repo) for case in plan.cases))
        if (
            self.plan_sha256 != plan.plan_sha256
            or plan.model_name != self.model.public_name
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

        input_rate = max_input_rate(self.expected_input_price)
        output_rate = rate_units(self.model.output_price)
        known = len(plan.cases) * self.known_case_reserve_units(
            plan.task_budget, input_rate=input_rate, output_rate=output_rate
        )
        unknown = (
            self.provider_config.max_request_bytes * input_rate
            + (self.provider_config.max_output_tokens + self.output_token_limit_margin)
            * output_rate
        )
        return format_amount(
            known + unknown * (len(plan.cases) if self.isolate_provider_failures else 1)
        )
