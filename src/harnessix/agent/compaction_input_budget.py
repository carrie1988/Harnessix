"""Pressure 压缩的完整 Provider wire 预付；不使用历史前缀折扣或发起请求。"""

from dataclasses import dataclass

from harnessix.agent.errors import KernelError
from harnessix.agent.model_input_budget import estimate_request_input_tokens
from harnessix.agent.models import Item, Thread, Turn, Usage
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.turn_budget_compaction import (
    pressure_summary_instructions,
    summary_output_limit,
    summary_request_item,
)
from harnessix.context.compaction import PreparedCompaction
from harnessix.context.compaction_contracts import CompactionSummary
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.contracts import PreparedContext
from harnessix.context.tool_result_view import PreparedModelHistory, history_document
from harnessix.models.contracts import (
    CompactionSummaryProjection,
    ModelInputTokenEstimator,
    ModelProvider,
    ModelRequest,
)


def reserved_summary_item(planned: PreparedCompaction) -> Item | None:
    """ASCII 占满 Native 投影预留；当前内置 mapper 的转义/Unicode 也受此 wire 上界支配。"""
    summary = CompactionSummary(compaction_id=planned.plan.compaction_id, text="x")
    wrapper_size = len(history_document(compaction_summary_item(summary)).encode()) - 1
    capacity = planned.plan.policy.summary_reserve_tokens - wrapper_size
    if capacity <= 0:
        return None
    return compaction_summary_item(
        CompactionSummary(compaction_id=planned.plan.compaction_id, text="x" * capacity)
    )


@dataclass(frozen=True, slots=True)
class CompactionInputBudget:
    provider: ModelProvider
    summary_provider: ModelProvider
    source_request: ModelRequest
    turn: Turn

    def _full_input(self, provider: ModelProvider, request: ModelRequest) -> int:
        # 改写窗口不能复用已付前缀；复用现有端口校验，但不给它任何历史用量锚点。
        unanchored = self.turn.model_copy(update={"model_attempts": ()})
        value = estimate_request_input_tokens(provider, request, unanchored, 0)
        if value <= 0:
            raise KernelError("invalid_provider_output", "Provider 完整输入估算能力发生漂移")
        return value

    def estimate_costs(
        self,
        planned: PreparedCompaction,
        config: CompactionRuntimeConfig,
        summary_instructions: str,
    ) -> tuple[int, int, int] | None:
        """返回摘要输入+输出、原请求输入、占满摘要预留的后继请求输入。"""
        output_limit = summary_output_limit(config, planned)
        summary_item = reserved_summary_item(planned)
        if output_limit <= 0 or summary_item is None:
            return None
        summary_request = ModelRequest(
            thread_id=self.source_request.thread_id,
            turn_id=self.source_request.turn_id,
            step=self.source_request.step,
            history=(summary_request_item(planned),),
            tools=(),
            instructions=pressure_summary_instructions(summary_instructions, config, planned),
            budget=self.turn.budget,
            remaining_tokens=output_limit,
        )
        retained = planned.retained_history
        successor = self.source_request.model_copy(
            update={"history": (retained[0], summary_item, *retained[1:])}
        )
        return (
            self._full_input(self.summary_provider, summary_request) + output_limit,
            self._full_input(self.provider, self.source_request),
            self._full_input(self.provider, successor),
        )


def prepare_compaction_input_budget(
    provider: ModelProvider | None,
    summary_provider: ModelProvider | None,
    thread: Thread,
    turn: Turn,
    prepared: PreparedModelHistory,
    selection: ModelToolSelection,
    context: PreparedContext | None,
) -> CompactionInputBudget | None:
    if not isinstance(provider, ModelInputTokenEstimator) or not isinstance(
        summary_provider, ModelInputTokenEstimator
    ):
        return None
    if (
        not isinstance(provider, CompactionSummaryProjection)
        or not isinstance(summary_provider, CompactionSummaryProjection)
        or provider.compaction_summary_projection != "native-document-json-string/v1"
        or summary_provider.compaction_summary_projection != "native-document-json-string/v1"
    ):
        return None
    output_limit = turn.budget.max_output_chars
    if context is not None:
        output_limit = min(output_limit, context.inspection.limits.reserved_output_tokens)
    request = ModelRequest(
        thread_id=thread.thread_id,
        turn_id=turn.turn_id,
        step=turn.model_steps + 1,
        history=prepared.history,
        tools=selection.tools,
        instructions=context.instructions if context else None,
        budget=turn.budget,
        # 只用于纯估算：所有预算提示数字取合法最大值，真实后继不能因位数增长超出预付。
        consumed_usage=Usage(),
        input_tokens_upper_bound=turn.budget.max_tokens,
        remaining_calls=turn.budget.max_steps,
        remaining_tokens=output_limit,
        tool_choice=selection.choice,
        forced_tool=selection.forced_tool,
    )
    return CompactionInputBudget(provider, summary_provider, request, turn)
