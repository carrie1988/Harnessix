"""SWE-bench 真实 Agent 执行的宿主、Provider 与费用边界。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import SWE_BENCH_CASE_COUNT, SweBenchPreRegistrationPlan
from harnessix.evals.swebench.public_test_profile import SweBenchCaseProcessProfile
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


class SweBenchAgentRunConfig(_SweBenchAgentRunSettings):
    """v2保持未知费用停止整轮及原完整轮次准入语义。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v2"] = (
        "harnessix.swebench-agent-run-config/v2"
    )


class SweBenchIsolatedAgentRunConfig(_SweBenchAgentRunSettings):
    """显式v3：只隔离已闭合的传输故障，逐Case预付、失败不重试。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v3"] = (
        "harnessix.swebench-agent-run-config/v3"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"

    @property
    def isolate_provider_failures(self) -> bool:
        return True


class SweBenchBudgetWindowAgentRunConfig(_SweBenchAgentRunSettings):
    """显式v4候选：保留v3隔离及逐Case预付，启用有摊销价值的预算窗口。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v4"] = (
        "harnessix.swebench-agent-run-config/v4"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True


class SweBenchThinkingAgentRunConfig(_SweBenchAgentRunSettings):
    """独立v5诊断：保留v4隔离、逐Case预付及压力压缩，显式启用有界思考。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v5"] = (
        "harnessix.swebench-agent-run-config/v5"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True

    @property
    def thinking_enabled(self) -> bool:
        return True

    @property
    def output_token_limit_margin(self) -> int:
        return 10


class SweBenchPlanningAgentRunConfig(_SweBenchAgentRunSettings):
    """独立v6：非思考模型原生规划，保留v4隔离、逐Case预付及压力压缩。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v6"] = (
        "harnessix.swebench-agent-run-config/v6"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    planning_policy: Literal["native-model-plan"] = "native-model-plan"

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True

    @property
    def planning_enabled(self) -> bool:
        return True

    @property
    def output_token_limit_margin(self) -> int:
        return 10


class SweBenchSearchSnapshotAgentRunConfig(_SweBenchAgentRunSettings):
    """独立v7候选：搜索同次原始读取摘要，不启用规划或改变模型条件。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v7"] = (
        "harnessix.swebench-agent-run-config/v7"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    search_snapshot_policy: Literal["same_observation_file_sha256"] = "same_observation_file_sha256"

    @property
    def grep_snapshots_enabled(self) -> bool:
        return True

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True


SweBenchRunConfig = (
    SweBenchAgentRunConfig
    | SweBenchIsolatedAgentRunConfig
    | SweBenchBudgetWindowAgentRunConfig
    | SweBenchThinkingAgentRunConfig
    | SweBenchPlanningAgentRunConfig
    | SweBenchSearchSnapshotAgentRunConfig
)


def _validate_search_snapshot_mode(
    enabled: bool, *, isolated: bool, pressure_window: bool, planning: bool, thinking: bool
) -> None:
    """单因素候选不能静默混入其他策略或失去原费用隔离边界。"""
    if enabled and (planning or thinking or not (isolated and pressure_window)):
        raise ValueError("SWE-bench搜索快照必须保留故障隔离、预算窗口并关闭规划和思考")


def _run_config_type(
    *,
    isolate_provider_failures: bool,
    budget_pressure_compaction: bool,
    enable_thinking: bool,
    enable_planning: bool,
    enable_grep_snapshots: bool,
) -> type[SweBenchRunConfig]:
    """拒绝隐式模式组合，版本选择不改变旧配置序列化。"""
    flags = (
        (isolate_provider_failures, "故障隔离"),
        (budget_pressure_compaction, "预算窗口"),
        (enable_thinking, "思考诊断"),
        (enable_planning, "规划诊断"),
        (enable_grep_snapshots, "搜索快照"),
    )
    for value, name in flags:
        if type(value) is not bool:
            raise ValueError(f"SWE-bench{name}必须显式使用布尔开关")
    _validate_search_snapshot_mode(
        enable_grep_snapshots,
        isolated=isolate_provider_failures,
        pressure_window=budget_pressure_compaction,
        planning=enable_planning,
        thinking=enable_thinking,
    )
    if enable_planning and (
        enable_thinking or not (isolate_provider_failures and budget_pressure_compaction)
    ):
        raise ValueError("SWE-bench规划诊断必须显式启用故障隔离与预算窗口并关闭思考")
    if enable_thinking and not (isolate_provider_failures and budget_pressure_compaction):
        raise ValueError("SWE-bench思考诊断必须显式启用故障隔离与预算窗口")
    if budget_pressure_compaction and not isolate_provider_failures:
        raise ValueError("SWE-bench预算窗口候选必须显式保留故障隔离与逐Case预付")
    if enable_planning:
        return SweBenchPlanningAgentRunConfig
    if enable_grep_snapshots:
        return SweBenchSearchSnapshotAgentRunConfig
    if enable_thinking:
        return SweBenchThinkingAgentRunConfig
    if budget_pressure_compaction:
        return SweBenchBudgetWindowAgentRunConfig
    if isolate_provider_failures:
        return SweBenchIsolatedAgentRunConfig
    return SweBenchAgentRunConfig


def build_swebench_agent_run_config(
    plan: SweBenchPreRegistrationPlan,
    *,
    source_root: Path,
    work_root: Path,
    cache_root: Path,
    git_executable: Path,
    case_process_profiles: tuple[SweBenchCaseProcessProfile, ...],
    fee_stop_amount: Amount,
    api_key_env: str = "DASHSCOPE_API_KEY",
    observed_at: datetime | None = None,
    isolate_provider_failures: bool = False,
    budget_pressure_compaction: bool = False,
    enable_thinking: bool = False,
    enable_planning: bool = False,
    enable_grep_snapshots: bool = False,
) -> SweBenchRunConfig:
    """按已求证的北京 PAYG 价格创建可持久化执行绑定。"""

    now = observed_at or datetime.now(UTC)
    parameters = {item.name: item.value for item in plan.model_settings.parameters}
    config_model = _run_config_type(
        isolate_provider_failures=isolate_provider_failures,
        budget_pressure_compaction=budget_pressure_compaction,
        enable_thinking=enable_thinking,
        enable_planning=enable_planning,
        enable_grep_snapshots=enable_grep_snapshots,
    )
    config = config_model(
        plan_sha256=plan.plan_sha256,
        source_root=str(source_root),
        work_root=str(work_root),
        cache_root=str(cache_root),
        git_executable=str(git_executable),
        provider_config=OpenAIChatConfig(
            base_url=_BASE_URL,
            model=_MODEL,
            api_key_env=api_key_env,
            capabilities=ChatCapabilities(
                tool_calls=True,
                parallel_tool_calls=parameters.get("parallel_tool_calls") is True,
            ),
            max_output_tokens=4096,
            timeout_seconds=plan.task_budget.timeout_seconds,
            io_timeout_seconds=180,
            max_attempts=1,
            retry_delay_seconds=0,
            max_request_bytes=250_000,
            max_response_bytes=4 * 1024 * 1024,
            output_token_parameter="max_completion_tokens" if enable_thinking else "max_tokens",
            enable_thinking=enable_thinking,
            thinking_budget=1024 if enable_thinking else None,
        ),
        price=PriceSnapshot(
            version=f"aliyun-bailian-{now.date().isoformat()}-qwen3-8-max",
            source_url=_PRICE_URL,
            billing_provider="aliyun-bailian",
            model=_MODEL,
            region="cn-beijing",
            service_tier="payg",
            inference_mode="thinking" if enable_thinking else "non-thinking",
            currency=_CURRENCY,
            valid_from=now,
            valid_until=now + _PRICE_VALIDITY,
            input_tokens_min=0,
            input_tokens_max=983_616 if enable_thinking else 991_808,
            input_price=FlatInputPrice(per_million=_INPUT_PRICE),
            output_per_million=_OUTPUT_PRICE,
        ),
        billing_context=BillingContext(
            billing_provider="aliyun-bailian",
            region="cn-beijing",
            service_tier="payg",
            inference_mode="thinking" if enable_thinking else "non-thinking",
        ),
        fee_stop_amount=fee_stop_amount,
        case_process_profiles=case_process_profiles,
    )
    config.require_plan(plan)
    return config
