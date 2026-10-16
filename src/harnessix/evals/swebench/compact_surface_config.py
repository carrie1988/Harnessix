"""工作集视图＋紧凑模型表面组合候选；固定任务预算、费用隔离与认证回收不变。"""

from typing import Literal

from pydantic import Field

from harnessix.evals.swebench.run_settings import _SweBenchAgentRunSettings


class SweBenchCompactSurfaceAgentRunConfig(_SweBenchAgentRunSettings):
    spec_version: Literal["harnessix.swebench-agent-run-config/v14"] = (
        "harnessix.swebench-agent-run-config/v14"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    history_view_policy: Literal["recent_tool_result_groups"] = "recent_tool_result_groups"
    retain_recent_result_groups: int = Field(ge=1, le=64, strict=True)
    model_surface_policy: Literal["compact_patch_and_instructions"] = (
        "compact_patch_and_instructions"
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
    def working_set_result_groups(self) -> int | None:
        return self.retain_recent_result_groups

    @property
    def compact_model_surface(self) -> bool:
        return True


class SweBenchConvergenceAgentRunConfig(_SweBenchAgentRunSettings):
    """v14组合之上再显式加入收敛指引与可负担请求提示；其他边界与v14一致。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v15"] = (
        "harnessix.swebench-agent-run-config/v15"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    history_view_policy: Literal["recent_tool_result_groups"] = "recent_tool_result_groups"
    retain_recent_result_groups: int = Field(ge=1, le=64, strict=True)
    model_surface_policy: Literal["compact_patch_and_instructions"] = (
        "compact_patch_and_instructions"
    )
    convergence_policy: Literal["patch_once_cause_observed_with_affordable_hint"] = (
        "patch_once_cause_observed_with_affordable_hint"
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
    def working_set_result_groups(self) -> int | None:
        return self.retain_recent_result_groups

    @property
    def compact_model_surface(self) -> bool:
        return True

    @property
    def convergence_guidance(self) -> bool:
        return True


class SweBenchPatchDeadlineAgentRunConfig(_SweBenchAgentRunSettings):
    """v15之上显式加入按可负担请求数的首次Patch截止；其他边界与v15一致。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v16"] = (
        "harnessix.swebench-agent-run-config/v16"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    history_view_policy: Literal["recent_tool_result_groups"] = "recent_tool_result_groups"
    retain_recent_result_groups: int = Field(ge=1, le=64, strict=True)
    model_surface_policy: Literal["compact_patch_and_instructions"] = (
        "compact_patch_and_instructions"
    )
    convergence_policy: Literal["patch_once_cause_observed_with_affordable_hint"] = (
        "patch_once_cause_observed_with_affordable_hint"
    )
    patch_deadline_requests: int = Field(ge=1, le=16, strict=True)

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
    def working_set_result_groups(self) -> int | None:
        return self.retain_recent_result_groups

    @property
    def compact_model_surface(self) -> bool:
        return True

    @property
    def convergence_guidance(self) -> bool:
        return True

    @property
    def patch_deadline(self) -> int | None:
        return self.patch_deadline_requests


class SweBenchBudgetTruncationAgentRunConfig(SweBenchPatchDeadlineAgentRunConfig):
    """v16之上显式把剩余预算压低上限造成的截断归为预算耗尽；其他边界与v16一致。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v17"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v17"
    )
    truncation_policy: Literal["budget_capped_truncation_is_budget_exhaustion"] = (
        "budget_capped_truncation_is_budget_exhaustion"
    )

    @property
    def budget_capped_truncation(self) -> bool:
        return True


class SweBenchPromptCacheAgentRunConfig(SweBenchBudgetTruncationAgentRunConfig):
    """v17之上显式开启前缀缓存布局、分区缓存计价与字符串化容器参数还原；其他边界与v17一致。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v18"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v18"
    )
    cache_policy: Literal["explicit_tail_prefix_partitioned_price"] = (
        "explicit_tail_prefix_partitioned_price"
    )
    argument_policy: Literal["restore_stringified_schema_containers"] = (
        "restore_stringified_schema_containers"
    )

    @property
    def prompt_cache(self) -> bool:
        return True

    @property
    def normalize_stringified_arguments(self) -> bool:
        return True


class SweBenchFullCachedHistoryAgentRunConfig(SweBenchPromptCacheAgentRunConfig):
    """v18之上关闭工作集遮蔽：历史只追加，已读内容保持可见且整段命中前缀缓存。

    遮蔽每步改写一组旧结果会使其后全部前缀按创建价重建，并迫使模型重读已读源码。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v19"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v19"
    )
    history_view_policy: Literal["full_cached_history"] = "full_cached_history"  # type: ignore[assignment]
    retain_recent_result_groups: None = None  # type: ignore[assignment]


class SweBenchCachedClosureAgentRunConfig(SweBenchFullCachedHistoryAgentRunConfig):
    """v19之上：整轮稳定工具目录、净改动为空时重新开放修改、关闭预算压力摘要。

    阶段收窄目录会使整段前缀缓存失效；压力摘要改写只追加历史且其溢出会终止Case。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v20"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v20"
    )
    context_budget_policy: Literal["cached_history_without_pressure_window"] = (  # type: ignore[assignment]
        "cached_history_without_pressure_window"
    )
    catalog_policy: Literal["stable_turn_catalog"] = "stable_turn_catalog"
    workflow_policy: Literal["reopen_after_clean_review"] = "reopen_after_clean_review"

    @property
    def budget_pressure_compaction(self) -> bool:
        return False

    @property
    def stable_tool_catalog(self) -> bool:
        return True

    @property
    def reopen_after_clean_review(self) -> bool:
        return True


class SweBenchScratchReproductionAgentRunConfig(SweBenchCachedClosureAgentRunConfig):
    """v20之上：先在复现草稿目录写失败测试再修复；草稿永不进入交付补丁或完成判断。"""

    spec_version: Literal["harnessix.swebench-agent-run-config/v21"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v21"
    )
    reproduction_policy: Literal["scratch_reproduction_test"] = "scratch_reproduction_test"

    @property
    def scratch_reproduction(self) -> bool:
        return True


class SweBenchProtocolRobustAgentRunConfig(SweBenchScratchReproductionAgentRunConfig):
    """v21之上：说明单响应工具调用上限；调用组发布前失败的协议输出按足额预留隔离，不停整轮。

    v53因一次响应发出超过上限的并行调用，流在用量前中止、费用未知而整轮停止。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v22"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v22"
    )
    provider_failure_policy: Literal["freeze_transport_and_protocol_with_full_reserve"] = (  # type: ignore[assignment]
        "freeze_transport_and_protocol_with_full_reserve"
    )
    tool_call_policy: Literal["state_per_response_cap"] = "state_per_response_cap"

    @property
    def isolate_protocol_failures(self) -> bool:
        return True

    @property
    def state_tool_call_cap(self) -> bool:
        return True


class SweBenchExplainedGatesAgentRunConfig(SweBenchProtocolRobustAgentRunConfig):
    """v22之上：工作流视图向模型说明本步工具受限原因与解锁条件；可执行集合与门禁不变。

    v54中17%的请求调用了当前阶段不可调用的工具，只收到“工具未注册”，不知应先重读改动文件。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v23"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v23"
    )
    tool_gate_policy: Literal["explain_restricted_tools"] = "explain_restricted_tools"
    reserve_policy: Literal["bounded_output_reserve"] = "bounded_output_reserve"

    @property
    def explain_tool_gates(self) -> bool:
        return True

    @property
    def bounded_output_reserve(self) -> bool:
        return True


class SweBenchConnectRetryAgentRunConfig(SweBenchExplainedGatesAgentRunConfig):
    """v23之上：TCP连接或TLS握手失败时重连至多3次；请求尚未发出，不重采样、不重复计费。

    v47与d2各因本机代理的瞬时连接失败丢失整题（httpx_connect_failure）。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v24"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v24"
    )
    connection_policy: Literal["retry_connection_establishment"] = "retry_connection_establishment"

    @property
    def connect_retries(self) -> int:
        return 3


class SweBenchForcedChoiceTolerantAgentRunConfig(SweBenchConnectRetryAgentRunConfig):
    """v24之上：强制工具选择下调用其他工具时，违约调用沿目录拒绝路径记为无效果失败并在原预算内纠正。

    d3中DeepSeek在强制git_review时调用了其他工具，前两项均因此整Turn失败（补丁已完成）。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v25"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v25"
    )
    forced_choice_policy: Literal["reject_violating_calls"] = "reject_violating_calls"

    @property
    def reject_forced_choice_violations(self) -> bool:
        return True


class SweBenchArgumentRepairAgentRunConfig(SweBenchForcedChoiceTolerantAgentRunConfig):
    """v25之上：工具参数严格解析失败时只修复原始控制字符与非法反斜杠转义，其余畸形仍为协议失败；
    自动前缀缓存模型改用stable_tail布局（逐请求说明移到末尾）。

    d4中DeepSeek的畸形参数JSON以tool_arguments_invalid使整Turn失败；说明插在指令与历史之间，
    前缀只命中指令部分（缓存命中10%～38%）。
    """

    spec_version: Literal["harnessix.swebench-agent-run-config/v26"] = (  # type: ignore[assignment]
        "harnessix.swebench-agent-run-config/v26"
    )
    argument_wire_policy: Literal["repair_escapes_and_control_characters"] = (
        "repair_escapes_and_control_characters"
    )

    cache_layout_policy: Literal["stable_tail_for_automatic_cache"] = (
        "stable_tail_for_automatic_cache"
    )

    @property
    def repair_tool_arguments(self) -> bool:
        return True

    @property
    def stable_cache_tail(self) -> bool:
        return True
