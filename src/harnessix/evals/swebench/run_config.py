"""显式选择并构建SWE-bench运行配置版本；旧配置序列化不随候选改变。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.public_test_profile import SweBenchCaseProcessProfile
from harnessix.evals.swebench.run_settings import (
    _BASE_URL,
    _CURRENCY,
    _INPUT_PRICE,
    _MODEL,
    _OUTPUT_PRICE,
    _PRICE_URL,
    _PRICE_VALIDITY,
    _SweBenchAgentRunSettings,
)
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.pricing import Amount, BillingContext, FlatInputPrice, PriceSnapshot


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


class SweBenchOptionalPreEditAgentRunConfig(_SweBenchAgentRunSettings):
    """独立v8：修改前可选原受控Profile，补丁后验证仍为必需。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v8"] = (
        "harnessix.swebench-agent-run-config/v8"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    pre_edit_profile_policy: Literal["optional_explicit_public_targets"] = (
        "optional_explicit_public_targets"
    )

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True

    @property
    def optional_pre_edit_profile_enabled(self) -> bool:
        return True


class SweBenchRecyclingAgentRunConfig(_SweBenchAgentRunSettings):
    """显式v9宿主生命周期；策略和回收均进入冻结指纹，旧版本不自动删除。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v9"] = (
        "harnessix.swebench-agent-run-config/v9"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    agent_strategy: Literal["standard", "search_snapshot", "optional_pre_edit"] = "standard"

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True

    @property
    def workspace_retirement_enabled(self) -> bool:
        return True

    @property
    def grep_snapshots_enabled(self) -> bool:
        return self.agent_strategy == "search_snapshot"

    @property
    def optional_pre_edit_profile_enabled(self) -> bool:
        return self.agent_strategy == "optional_pre_edit"


class SweBenchRegexAgentRunConfig(_SweBenchAgentRunSettings):
    """独立v10：有界RE2正则/显式字面搜索，保持原预算与认证回收。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v10"] = (
        "harnessix.swebench-agent-run-config/v10"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    search_policy: Literal["bounded_re2_regex_explicit_literal"] = (
        "bounded_re2_regex_explicit_literal"
    )

    @property
    def isolate_provider_failures(self) -> bool:
        return True

    @property
    def budget_pressure_compaction(self) -> bool:
        return True

    @property
    def workspace_retirement_enabled(self) -> bool:
        return True

    @property
    def regex_search_enabled(self) -> bool:
        return True


SweBenchRunConfig = (
    SweBenchAgentRunConfig
    | SweBenchIsolatedAgentRunConfig
    | SweBenchBudgetWindowAgentRunConfig
    | SweBenchThinkingAgentRunConfig
    | SweBenchPlanningAgentRunConfig
    | SweBenchSearchSnapshotAgentRunConfig
    | SweBenchOptionalPreEditAgentRunConfig
    | SweBenchRecyclingAgentRunConfig
    | SweBenchRegexAgentRunConfig
)


def _validate_isolated_coding_candidate(
    enabled: bool, *, isolated: bool, pressure_window: bool, planning: bool, thinking: bool
) -> None:
    """单因素候选不能静默混入其他策略或失去原费用隔离边界。"""
    if enabled and (planning or thinking or not (isolated and pressure_window)):
        raise ValueError("SWE-bench工具闭环候选必须保留故障隔离、预算窗口并关闭规划和思考")


def _run_config_type(
    *,
    isolate_provider_failures: bool,
    budget_pressure_compaction: bool,
    enable_thinking: bool,
    enable_planning: bool,
    enable_grep_snapshots: bool,
    allow_optional_pre_edit_profile: bool,
    recycle_workspaces: bool,
    enable_regex_search: bool,
) -> type[SweBenchRunConfig]:
    """拒绝隐式模式组合，版本选择不改变旧配置序列化。"""
    flags = (
        (isolate_provider_failures, "故障隔离"),
        (budget_pressure_compaction, "预算窗口"),
        (enable_thinking, "思考诊断"),
        (enable_planning, "规划诊断"),
        (enable_grep_snapshots, "搜索快照"),
        (allow_optional_pre_edit_profile, "可选预测试"),
        (recycle_workspaces, "工作区回收"),
        (enable_regex_search, "正则搜索"),
    )
    for value, name in flags:
        if type(value) is not bool:
            raise ValueError(f"SWE-bench{name}必须显式使用布尔开关")
    _validate_isolated_coding_candidate(
        enable_grep_snapshots
        or allow_optional_pre_edit_profile
        or recycle_workspaces
        or enable_regex_search,
        isolated=isolate_provider_failures,
        pressure_window=budget_pressure_compaction,
        planning=enable_planning,
        thinking=enable_thinking,
    )
    if enable_grep_snapshots and allow_optional_pre_edit_profile:
        raise ValueError("SWE-bench可选预测试与搜索快照必须独立冻结，不隐式组合候选")
    if enable_regex_search:
        if enable_grep_snapshots or allow_optional_pre_edit_profile or not recycle_workspaces:
            raise ValueError("SWE-bench正则搜索需保留认证回收，不隐式混入其他工具候选")
        return SweBenchRegexAgentRunConfig
    if recycle_workspaces:
        return SweBenchRecyclingAgentRunConfig
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
    if allow_optional_pre_edit_profile:
        return SweBenchOptionalPreEditAgentRunConfig
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
    allow_optional_pre_edit_profile: bool = False,
    recycle_workspaces: bool = False,
    enable_regex_search: bool = False,
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
        allow_optional_pre_edit_profile=allow_optional_pre_edit_profile,
        recycle_workspaces=recycle_workspaces,
        enable_regex_search=enable_regex_search,
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
    if isinstance(config, SweBenchRecyclingAgentRunConfig):
        strategy: Literal["standard", "search_snapshot", "optional_pre_edit"] = "standard"
        if enable_grep_snapshots:
            strategy = "search_snapshot"
        elif allow_optional_pre_edit_profile:
            strategy = "optional_pre_edit"
        config = SweBenchRecyclingAgentRunConfig.model_validate(
            {**config.model_dump(), "agent_strategy": strategy}
        )
    config.require_plan(plan)
    return config
