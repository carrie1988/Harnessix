"""显式选择并构建SWE-bench运行配置版本；旧配置序列化不随候选改变。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from harnessix.evals.swebench.compact_surface_config import (
    SweBenchArgumentRepairAgentRunConfig,
    SweBenchBoundedRepairAgentRunConfig,
    SweBenchBudgetTruncationAgentRunConfig,
    SweBenchCachedClosureAgentRunConfig,
    SweBenchCompactSurfaceAgentRunConfig,
    SweBenchConnectRetryAgentRunConfig,
    SweBenchConvergenceAgentRunConfig,
    SweBenchExplainedGatesAgentRunConfig,
    SweBenchForcedChoiceTolerantAgentRunConfig,
    SweBenchFullCachedHistoryAgentRunConfig,
    SweBenchNativeThinkingAgentRunConfig,
    SweBenchPatchDeadlineAgentRunConfig,
    SweBenchPromptCacheAgentRunConfig,
    SweBenchProtocolRobustAgentRunConfig,
    SweBenchReadAfterFailedPatchAgentRunConfig,
    SweBenchScratchReproductionAgentRunConfig,
    SweBenchSelfVerificationAgentRunConfig,
    SweBenchTestTimeoutAgentRunConfig,
    SweBenchTruncationQualityAgentRunConfig,
)
from harnessix.evals.swebench.contracts import SweBenchPreRegistrationPlan
from harnessix.evals.swebench.definition_config import SweBenchDefinitionAgentRunConfig
from harnessix.evals.swebench.public_test_profile import SweBenchCaseProcessProfile
from harnessix.evals.swebench.run_settings import (
    _CURRENCY,
    _NATIVE_THINKING_OUTPUT,
    _PRICE_VALIDITY,
    MODEL_CATALOG,
    SweBenchModel,
    _SweBenchAgentRunSettings,
    _wire_prompt_cache,
    catalog_input_price,
)
from harnessix.evals.swebench.working_set_config import SweBenchWorkingSetAgentRunConfig
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.pricing import (
    Amount,
    BillingContext,
    PriceSnapshot,
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


class SweBenchClosingAgentRunConfig(_SweBenchAgentRunSettings):
    """独立v11：从持久请求事实派生调查收尾偏好，不扩张任务预算。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v11"] = (
        "harnessix.swebench-agent-run-config/v11"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    closing_policy: Literal["observed_input_four_call_closing_preference"] = (
        "observed_input_four_call_closing_preference"
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
    def closing_phase_enabled(self) -> bool:
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
    | SweBenchClosingAgentRunConfig
    | SweBenchDefinitionAgentRunConfig
    | SweBenchWorkingSetAgentRunConfig
    | SweBenchCompactSurfaceAgentRunConfig
    | SweBenchConvergenceAgentRunConfig
    | SweBenchPatchDeadlineAgentRunConfig
    | SweBenchBudgetTruncationAgentRunConfig
    | SweBenchPromptCacheAgentRunConfig
    | SweBenchFullCachedHistoryAgentRunConfig
    | SweBenchCachedClosureAgentRunConfig
    | SweBenchScratchReproductionAgentRunConfig
    | SweBenchProtocolRobustAgentRunConfig
    | SweBenchExplainedGatesAgentRunConfig
    | SweBenchConnectRetryAgentRunConfig
    | SweBenchForcedChoiceTolerantAgentRunConfig
    | SweBenchArgumentRepairAgentRunConfig
    | SweBenchTestTimeoutAgentRunConfig
    | SweBenchReadAfterFailedPatchAgentRunConfig
    | SweBenchTruncationQualityAgentRunConfig
    | SweBenchNativeThinkingAgentRunConfig
    | SweBenchSelfVerificationAgentRunConfig
    | SweBenchBoundedRepairAgentRunConfig
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
    reserve_closing_phase: bool,
    enable_definition_views: bool,
    working_set: bool,
    compact_model_surface: bool,
    convergence_guidance: bool,
    patch_deadline: bool,
    budget_capped_truncation: bool = False,
    prompt_cache: bool = False,
    cached_closure_workflow: bool = False,
    scratch_reproduction: bool = False,
    protocol_robust: bool = False,
    explained_gates: bool = False,
    connect_retry: bool = False,
    forced_choice_tolerant: bool = False,
    argument_repair: bool = False,
    test_timeout_failures: bool = False,
    reads_after_failed_patch: bool = False,
    truncation_quality: bool = False,
    native_thinking: bool = False,
    self_verification: bool = False,
    bounded_repair: bool = False,
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
        (reserve_closing_phase, "调查收尾"),
        (enable_definition_views, "定义视图"),
        (working_set, "工作集视图"),
        (compact_model_surface, "紧凑模型表面"),
        (convergence_guidance, "收敛指引"),
        (patch_deadline, "Patch截止"),
        (budget_capped_truncation, "预算截断归类"),
        (prompt_cache, "前缀缓存"),
        (cached_closure_workflow, "缓存收尾工作流"),
        (scratch_reproduction, "复现草稿"),
        (protocol_robust, "协议失败隔离"),
        (explained_gates, "工具门禁说明"),
        (connect_retry, "连接重试"),
        (forced_choice_tolerant, "强制选择违约拒绝"),
        (argument_repair, "工具参数修复"),
        (test_timeout_failures, "测试超时判定"),
        (reads_after_failed_patch, "失败Patch后保留读取"),
        (truncation_quality, "截断计质量失败"),
        (native_thinking, "原生思考"),
        (self_verification, "自我校验"),
        (bounded_repair, "修复循环上限"),
    )
    for value, name in flags:
        if type(value) is not bool:
            raise ValueError(f"SWE-bench{name}必须显式使用布尔开关")
    _validate_isolated_coding_candidate(
        enable_grep_snapshots
        or allow_optional_pre_edit_profile
        or recycle_workspaces
        or enable_regex_search
        or reserve_closing_phase
        or enable_definition_views
        or working_set
        or compact_model_surface,
        isolated=isolate_provider_failures,
        # v20以稳定缓存历史替代压力窗口；其余候选仍须保留预算窗口。
        pressure_window=budget_pressure_compaction or cached_closure_workflow,
        planning=enable_planning,
        thinking=enable_thinking,
    )
    if cached_closure_workflow and (budget_pressure_compaction or working_set or not prompt_cache):
        raise ValueError("SWE-bench缓存收尾工作流只在前缀缓存且无工作集、无压力窗口时冻结")
    if bounded_repair and not self_verification:
        raise ValueError("SWE-bench修复循环上限只在自我校验组合之上冻结")
    if self_verification and (native_thinking or not truncation_quality):
        raise ValueError("SWE-bench自我校验只在截断计质量失败组合之上冻结，暂不与原生思考组合")
    if native_thinking and not truncation_quality:
        raise ValueError("SWE-bench原生思考只在截断计质量失败组合之上冻结")
    if truncation_quality and not reads_after_failed_patch:
        raise ValueError("SWE-bench截断计质量失败只在失败Patch后保留读取组合之上冻结")
    if reads_after_failed_patch and not test_timeout_failures:
        raise ValueError("SWE-bench失败Patch后保留读取只在测试超时判定组合之上冻结")
    if test_timeout_failures and not argument_repair:
        raise ValueError("SWE-bench测试超时判定只在工具参数修复组合之上冻结")
    if argument_repair and not forced_choice_tolerant:
        raise ValueError("SWE-bench工具参数修复只在强制选择违约拒绝组合之上冻结")
    if forced_choice_tolerant and not connect_retry:
        raise ValueError("SWE-bench强制选择违约拒绝只在连接重试组合之上冻结")
    if connect_retry and not explained_gates:
        raise ValueError("SWE-bench连接重试只在工具门禁说明组合之上冻结")
    if explained_gates and not protocol_robust:
        raise ValueError("SWE-bench工具门禁说明只在协议失败隔离组合之上冻结")
    if protocol_robust and not scratch_reproduction:
        raise ValueError("SWE-bench协议失败隔离只在复现草稿组合之上冻结")
    if scratch_reproduction and not cached_closure_workflow:
        raise ValueError("SWE-bench复现草稿只在缓存收尾工作流之上冻结")
    if enable_grep_snapshots and allow_optional_pre_edit_profile:
        raise ValueError("SWE-bench可选预测试与搜索快照必须独立冻结，不隐式组合候选")
    if working_set:
        if (
            enable_grep_snapshots
            or enable_regex_search
            or allow_optional_pre_edit_profile
            or reserve_closing_phase
            or enable_definition_views
            or not recycle_workspaces
        ):
            raise ValueError("SWE-bench工作集视图需认证回收，不混入其他工具候选")
        if convergence_guidance and not compact_model_surface:
            raise ValueError("SWE-bench收敛指引只与工作集和紧凑模型表面组合冻结")
        if patch_deadline and not convergence_guidance:
            raise ValueError("SWE-bench Patch截止只在收敛指引组合之上冻结")
        if budget_capped_truncation and not patch_deadline:
            raise ValueError("SWE-bench预算截断归类只在Patch截止组合之上冻结")
        if prompt_cache and not budget_capped_truncation:
            raise ValueError("SWE-bench前缀缓存只在预算截断归类组合之上冻结")
        if prompt_cache:
            return SweBenchPromptCacheAgentRunConfig
        if budget_capped_truncation:
            return SweBenchBudgetTruncationAgentRunConfig
        if patch_deadline:
            return SweBenchPatchDeadlineAgentRunConfig
        if convergence_guidance:
            return SweBenchConvergenceAgentRunConfig
        if compact_model_surface:
            return SweBenchCompactSurfaceAgentRunConfig
        return SweBenchWorkingSetAgentRunConfig
    if (
        prompt_cache
        and compact_model_surface
        and convergence_guidance
        and patch_deadline
        and budget_capped_truncation
        and recycle_workspaces
        and not (
            enable_grep_snapshots
            or enable_regex_search
            or allow_optional_pre_edit_profile
            or reserve_closing_phase
            or enable_definition_views
        )
    ):
        # 前缀缓存下关闭工作集：历史只追加，避免遮蔽改写前缀与重复读取。
        if bounded_repair:
            return SweBenchBoundedRepairAgentRunConfig
        if self_verification:
            return SweBenchSelfVerificationAgentRunConfig
        if native_thinking:
            return SweBenchNativeThinkingAgentRunConfig
        if truncation_quality:
            return SweBenchTruncationQualityAgentRunConfig
        if reads_after_failed_patch:
            return SweBenchReadAfterFailedPatchAgentRunConfig
        if test_timeout_failures:
            return SweBenchTestTimeoutAgentRunConfig
        if argument_repair:
            return SweBenchArgumentRepairAgentRunConfig
        if forced_choice_tolerant:
            return SweBenchForcedChoiceTolerantAgentRunConfig
        if connect_retry:
            return SweBenchConnectRetryAgentRunConfig
        if explained_gates:
            return SweBenchExplainedGatesAgentRunConfig
        if protocol_robust:
            return SweBenchProtocolRobustAgentRunConfig
        if scratch_reproduction:
            return SweBenchScratchReproductionAgentRunConfig
        if cached_closure_workflow:
            return SweBenchCachedClosureAgentRunConfig
        return SweBenchFullCachedHistoryAgentRunConfig
    if (
        compact_model_surface
        or convergence_guidance
        or patch_deadline
        or budget_capped_truncation
        or prompt_cache
    ):
        raise ValueError("SWE-bench紧凑模型表面或收敛指引只与工作集视图组合冻结")
    if enable_definition_views:
        if (
            enable_grep_snapshots
            or enable_regex_search
            or allow_optional_pre_edit_profile
            or reserve_closing_phase
            or not recycle_workspaces
        ):
            raise ValueError("SWE-bench定义视图需认证回收，不混入其他工具候选")
        return SweBenchDefinitionAgentRunConfig
    if reserve_closing_phase:
        if (
            enable_grep_snapshots
            or enable_regex_search
            or allow_optional_pre_edit_profile
            or not recycle_workspaces
        ):
            raise ValueError("SWE-bench调查收尾候选需认证回收，不混入其他工具候选")
        return SweBenchClosingAgentRunConfig
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
    reserve_closing_phase: bool = False,
    enable_definition_views: bool = False,
    working_set_result_groups: int | None = None,
    compact_model_surface: bool = False,
    convergence_guidance: bool = False,
    patch_deadline_requests: int | None = None,
    budget_capped_truncation: bool = False,
    model: SweBenchModel = "qwen3.8-max",
    prompt_cache: bool = False,
    cached_closure_workflow: bool = False,
    scratch_reproduction: bool = False,
    protocol_robust: bool = False,
    explained_gates: bool = False,
    connect_retry: bool = False,
    forced_choice_tolerant: bool = False,
    argument_repair: bool = False,
    test_timeout_failures: bool = False,
    reads_after_failed_patch: bool = False,
    truncation_quality: bool = False,
    native_thinking: bool = False,
    self_verification: bool = False,
    bounded_repair: bool = False,
) -> SweBenchRunConfig:
    """按已求证的北京 PAYG 价格创建可持久化执行绑定。"""

    now = observed_at or datetime.now(UTC)
    catalog = MODEL_CATALOG[model]
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
        reserve_closing_phase=reserve_closing_phase,
        enable_definition_views=enable_definition_views,
        working_set=working_set_result_groups is not None,
        compact_model_surface=compact_model_surface,
        convergence_guidance=convergence_guidance,
        patch_deadline=patch_deadline_requests is not None,
        budget_capped_truncation=budget_capped_truncation,
        prompt_cache=prompt_cache,
        cached_closure_workflow=cached_closure_workflow,
        scratch_reproduction=scratch_reproduction,
        protocol_robust=protocol_robust,
        explained_gates=explained_gates,
        connect_retry=connect_retry,
        forced_choice_tolerant=forced_choice_tolerant,
        argument_repair=argument_repair,
        test_timeout_failures=test_timeout_failures,
        reads_after_failed_patch=reads_after_failed_patch,
        truncation_quality=truncation_quality,
        native_thinking=native_thinking,
        self_verification=self_verification,
        bounded_repair=bounded_repair,
    )
    extra: dict[str, object] = (
        {}
        if working_set_result_groups is None
        else {"retain_recent_result_groups": working_set_result_groups}
    )
    if patch_deadline_requests is not None:
        extra["patch_deadline_requests"] = patch_deadline_requests
    fields: dict[str, object] = dict(
        plan_sha256=plan.plan_sha256,
        source_root=str(source_root),
        work_root=str(work_root),
        cache_root=str(cache_root),
        git_executable=str(git_executable),
        provider_config=OpenAIChatConfig(
            base_url=catalog.base_url,
            model=model,
            api_key_env=api_key_env,
            capabilities=ChatCapabilities(
                tool_calls=True,
                parallel_tool_calls=parameters.get("parallel_tool_calls") is True,
            ),
            max_output_tokens=_NATIVE_THINKING_OUTPUT if native_thinking else 4096,
            timeout_seconds=plan.task_budget.timeout_seconds,
            io_timeout_seconds=180,
            max_attempts=1,
            retry_delay_seconds=0,
            max_request_bytes=250_000,
            max_response_bytes=4 * 1024 * 1024,
            output_token_parameter="max_completion_tokens" if enable_thinking else "max_tokens",
            enable_thinking=enable_thinking or native_thinking,
            thinking_budget=1024 if enable_thinking else None,
            thinking_format=catalog.thinking_format,
            connect_retries=3 if connect_retry else 0,
            repair_tool_arguments=argument_repair,
            prompt_cache=_wire_prompt_cache(
                catalog, prompt_cache=prompt_cache, stable_tail=argument_repair
            ),
        ),
        price=PriceSnapshot(
            version=f"{catalog.billing_provider}-{now.date().isoformat()}-{catalog.price_slug}",
            source_url=catalog.price_url,
            billing_provider=catalog.billing_provider,
            model=model,
            region=catalog.region,
            service_tier="payg",
            inference_mode="thinking" if enable_thinking or native_thinking else "non-thinking",
            currency=_CURRENCY,
            valid_from=now,
            valid_until=now + _PRICE_VALIDITY,
            input_tokens_min=0,
            input_tokens_max=983_616 if enable_thinking else catalog.input_tokens_max,
            input_price=catalog_input_price(catalog, prompt_cache=prompt_cache),
            output_per_million=catalog.output_price,
        ),
        billing_context=BillingContext(
            billing_provider=catalog.billing_provider,
            region=catalog.region,
            service_tier="payg",
            inference_mode="thinking" if enable_thinking or native_thinking else "non-thinking",
        ),
        fee_stop_amount=fee_stop_amount,
        case_process_profiles=case_process_profiles,
    )
    config = config_model.model_validate({**fields, **extra})
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
