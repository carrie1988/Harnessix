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
