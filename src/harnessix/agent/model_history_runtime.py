"""模型历史运行边界：在可并发Steering下验证并原子冻结下一步模型输入。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.compaction_input_budget import (
    CompactionInputBudget,
    prepare_compaction_input_budget,
)
from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.model_input_budget import estimate_model_input_tokens
from harnessix.agent.models import (
    EventDraft,
    ModelHistoryPrepared,
    Thread,
    Turn,
    TurnStatus,
)
from harnessix.agent.planning import planning_history_anchors
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.reducer import get_turn
from harnessix.agent.turn_budget_compaction import (
    closing_reserve,
    pressure_history_allocation,
    pressure_policies,
    pressure_summary_instructions,
    require_request_slots,
    summary_output_limit,
    summary_request_item,
)
from harnessix.artifacts.ports import BatchDiffPublisher
from harnessix.context.compaction import PreparedCompaction, plan_compaction
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.contracts import PreparedContext
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import PreparedModelHistory, history_document
from harnessix.models.contracts import ModelProvider
from harnessix.session.ports import SessionStore


@dataclass(frozen=True, slots=True)
class PreparedHistoryStep:
    thread: Thread
    prepared: PreparedModelHistory
    reactive_compaction_required: bool
    budget_context: PreparedContext | None = None
    budget_tool_selection: ModelToolSelection | None = None


class ModelHistoryHost(Protocol):
    store: SessionStore
    provider: ModelProvider
    _summary_provider: ModelProvider | None
    _batch_diffs: BatchDiffPublisher | None
    _tool_result_view_policy: ToolResultViewPolicy
    _compaction: CompactionRuntimeConfig | None
    _budget_pressure_compaction: bool
    _fault: Callable[[str], None]

    def _model_tools_for_turn(self, turn: Turn) -> ModelToolSelection: ...

    def _lock(self, thread_id: UUID) -> AbstractAsyncContextManager[None]: ...

    async def _run_compaction(
        self,
        thread: Thread,
        turn: Turn,
        prepared: PreparedModelHistory,
        token: CancelToken,
        planned: PreparedCompaction | None = None,
    ) -> Thread: ...

    async def _prepare_budget_context(
        self,
        thread: Thread,
        prepared: PreparedModelHistory,
        token: CancelToken,
        selection: ModelToolSelection,
    ) -> PreparedContext | None: ...

    async def _verify_history_artifacts(
        self,
        thread: Thread,
        prepared: PreparedModelHistory,
        token: CancelToken,
    ) -> None: ...


def _requires_compaction(
    host: ModelHistoryHost,
    prepared: PreparedModelHistory,
    reactive_compaction_required: bool,
) -> bool:
    if host._compaction is None:
        return False
    return (
        reactive_compaction_required
        or sum(len(history_document(item).encode()) for item in prepared.history)
        > host._compaction.trigger_history_tokens
    )


async def _budget_plan(
    host: ModelHistoryHost,
    thread: Thread,
    turn: Turn,
    prepared: PreparedModelHistory,
    token: CancelToken,
    summary_instructions: str,
) -> tuple[PreparedCompaction | None, PreparedContext | None, ModelToolSelection | None]:
    assert host._compaction is not None
    selection = deepcopy(host._model_tools_for_turn(turn))
    context = await host._prepare_budget_context(thread, prepared, token, selection)
    fixed = estimate_model_input_tokens(
        (), selection.tools, context.instructions if context else None
    )
    policies = pressure_policies(
        turn, host._compaction, fixed, estimate_model_input_tokens(prepared.history, (), None)
    )
    if not policies:
        return None, None, None
    wire_budget = prepare_compaction_input_budget(
        getattr(host, "provider", None),
        getattr(host, "_summary_provider", None),
        thread,
        turn,
        prepared,
        selection,
        context,
    )
    # 缩小摘要容量可能让贪心窗口回填旧组，反而失去wire摊销收益。先保留全部
    # 原候选的选择顺序；只有它们均不成立，再试最紧合法窗口，固定组仍完整保留。
    preferences = (None, 512) if pressure_history_allocation(turn, fixed) > 512 else (None,)
    for preference in preferences:
        for policy in policies:
            planned = await _prepaid_pressure_plan(
                host,
                thread,
                turn,
                token,
                policy,
                fixed,
                summary_instructions,
                wire_budget=wire_budget,
                preferred_history_tokens=preference,
            )
            if planned is not None:
                return planned, context, selection
    return None, None, None


async def _prepaid_pressure_plan(
    host: ModelHistoryHost,
    thread: Thread,
    turn: Turn,
    token: CancelToken,
    policy: CompactionPolicy,
    fixed: int,
    summary_instructions: str,
    *,
    wire_budget: CompactionInputBudget | None = None,
    preferred_history_tokens: int | None = None,
) -> PreparedCompaction | None:
    """每个纯候选都必须满足原完整组、摊销和摘要/下一请求/收尾预付边界。"""
    assert host._compaction is not None
    try:
        planned = await plan_compaction(
            thread,
            turn.model_steps + 1,
            host._tool_result_view_policy,
            policy,
            token,
            compaction_id=new_id(),
            anchors=planning_history_anchors(thread),
            preferred_history_tokens=(
                preferred_history_tokens
                if preferred_history_tokens is not None
                else min(
                    policy.target_history_tokens,
                    max(512, pressure_history_allocation(turn, fixed)),
                )
            ),
        )
    except KernelError as error:
        if error.code not in {
            "context_compaction_retained_overflow",
            "context_compaction_no_progress",
            "context_compaction_source_overflow",
        }:
            raise
        return None
    if (
        summary_output_limit(host._compaction, planned) == 0
        or len(planned.summary_source) > 1_000_000
    ):
        return None
    if wire_budget is not None:
        costs = wire_budget.estimate_costs(planned, host._compaction, summary_instructions)
        if costs is None:
            return None
        summary_cost, source_input, successor_input = costs
        savings = source_input - successor_input
    else:
        instructions = pressure_summary_instructions(
            summary_instructions, host._compaction, planned
        )
        summary_cost = estimate_model_input_tokens(
            (summary_request_item(planned),), (), instructions
        )
        summary_cost += summary_output_limit(host._compaction, planned)
        savings = (
            planned.plan.source_history_tokens
            - planned.plan.retained_history_tokens
            - planned.plan.policy.summary_reserve_tokens
        )
        successor_input = (
            planned.plan.retained_history_tokens
            + planned.plan.policy.summary_reserve_tokens
            + fixed
        )
    # 同一保守估算尺度判断摊销价值，不把微小窗口缩减当成值得额外付费的压缩。
    future_requests = min(3, require_request_slots(turn)) - 1
    if summary_cost > savings * future_requests:
        return None
    cost = summary_cost + successor_input + closing_reserve(turn)
    if cost > turn.budget.max_tokens - turn.usage.total_tokens:
        return None
    return planned


async def _commit_if_current(
    host: ModelHistoryHost,
    thread_id: UUID,
    turn_id: UUID,
    model_step: int,
    prepared: PreparedModelHistory,
    token: CancelToken,
) -> Thread | None:
    async with host._lock(thread_id):
        token.checkpoint()
        thread = await host.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        if turn.status in {TurnStatus.CANCELLING, TurnStatus.CANCELLED}:
            raise TurnCancelled
        current = prepare_active_model_history(thread, model_step, host._tool_result_view_policy)
        if current != prepared:
            return None
        return await (host._batch_diffs or host.store).append(
            thread_id,
            [
                EventDraft(
                    turn_id=turn_id,
                    payload=ModelHistoryPrepared(
                        inspection=prepared.inspection,
                        decisions=prepared.new_decisions,
                    ),
                )
            ],
            expected_sequence=thread.sequence,
        )


async def prepare_and_commit_model_history(
    host: ModelHistoryHost,
    thread_id: UUID,
    turn_id: UUID,
    model_step: int,
    token: CancelToken,
    *,
    reactive_compaction_required: bool,
    summary_instructions: str,
) -> PreparedHistoryStep:
    """验证后若Session发生变化则重备，直到同一快照完成原子提交。"""

    while True:
        token.checkpoint()
        thread = await host.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        prepared = prepare_active_model_history(thread, model_step, host._tool_result_view_policy)
        budget_context = None
        budget_selection = None
        planned = None
        required = _requires_compaction(host, prepared, reactive_compaction_required)
        if not required and host._compaction is not None and host._budget_pressure_compaction:
            planned, budget_context, budget_selection = await _budget_plan(
                host, thread, turn, prepared, token, summary_instructions
            )
        if required or planned is not None:
            thread = await host._run_compaction(thread, turn, prepared, token, planned)
            reactive_compaction_required = False
            prepared = prepare_active_model_history(
                thread, model_step, host._tool_result_view_policy
            )
        await host._verify_history_artifacts(thread, prepared, token)
        host._fault("runtime.after_history_artifacts_verified")
        committed = await _commit_if_current(host, thread_id, turn_id, model_step, prepared, token)
        if committed is not None:
            host._fault("runtime.after_model_history_prepared")
            return PreparedHistoryStep(
                committed,
                prepared,
                reactive_compaction_required,
                budget_context,
                budget_selection,
            )
