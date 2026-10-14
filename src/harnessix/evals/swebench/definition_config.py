"""独立定义视图候选；固定任务预算、费用隔离与认证回收不变。"""

from typing import Literal

from harnessix.evals.swebench.run_settings import _SweBenchAgentRunSettings


class SweBenchDefinitionAgentRunConfig(_SweBenchAgentRunSettings):
    spec_version: Literal["harnessix.swebench-agent-run-config/v12"] = (
        "harnessix.swebench-agent-run-config/v12"
    )
    provider_failure_policy: Literal["freeze_transport_with_full_reserve"] = (
        "freeze_transport_with_full_reserve"
    )
    fee_admission_policy: Literal["next_isolated_case"] = "next_isolated_case"
    context_budget_policy: Literal["amortized_pressure_window"] = "amortized_pressure_window"
    workspace_policy: Literal["sealed_terminal_retirement"] = "sealed_terminal_retirement"
    definition_policy: Literal["explicit_bounded_python_definition"] = (
        "explicit_bounded_python_definition"
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
    def definition_views_enabled(self) -> bool:
        return True
