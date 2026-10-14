"""独立工作集视图候选；固定任务预算、费用隔离与认证回收不变。"""

from typing import Literal

from pydantic import Field

from harnessix.evals.swebench.run_settings import _SweBenchAgentRunSettings


class SweBenchWorkingSetAgentRunConfig(_SweBenchAgentRunSettings):
    spec_version: Literal["harnessix.swebench-agent-run-config/v13"] = (
        "harnessix.swebench-agent-run-config/v13"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    history_view_policy: Literal["recent_tool_result_groups"] = "recent_tool_result_groups"
    retain_recent_result_groups: int = Field(ge=1, le=64, strict=True)

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
