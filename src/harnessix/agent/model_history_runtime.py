"""模型历史运行边界：在可并发Steering下验证并原子冻结下一步模型输入。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, TurnCancelled
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
from harnessix.agent.reducer import get_turn
from harnessix.agent.turn_budget_compaction import (
    closing_reserve,
    pressure_history_allocation,
    pressure_policy,
    pressure_summary_instructions,
    require_request_slots,
    summary_output_limit,
    summary_request_item,
)
from harnessix.artifacts.ports import BatchDiffPublisher
from harnessix.context.compaction import PreparedCompaction, plan_compaction
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.contracts import PreparedContext
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import PreparedModelHistory, history_document
from harnessix.domain.models import ToolDescriptor
from harnessix.session.ports import SessionStore


@dataclass(frozen=True, slots=True)
class PreparedHistoryStep:
    thread: Thread
    prepared: PreparedModelHistory
    reactive_compaction_required: bool
    budget_context: PreparedContext | None = None


class ModelHistoryHost(Protocol):
    store: SessionStore
    _batch_diffs: BatchDiffPublisher | None
    _tool_result_view_policy: ToolResultViewPolicy
    _compaction: CompactionRuntimeConfig | None
    _budget_pressure_compaction: bool
    _fault: Callable[[str], None]
    _definitions: dict[str, ToolDescriptor]

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
        self, thread: Thread, prepared: PreparedModelHistory, token: CancelToken
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
) -> tuple[PreparedCompaction | None, PreparedContext | None]:
    assert host._compaction is not None
    context = await host._prepare_budget_context(thread, prepared, token)
    fixed = estimate_model_input_tokens(
        (), tuple(host._definitions.values()), context.instructions if context else None
    )
    policy = pressure_policy(
        turn, host._compaction, fixed, estimate_model_input_tokens(prepared.history, (), None)
    )
    if policy is None:
        return None, None
    try:
        planned = await plan_compaction(
            thread,
            turn.model_steps + 1,
            host._tool_result_view_policy,
            policy,
            token,
            compaction_id=new_id(),
            preferred_history_tokens=min(
                policy.target_history_tokens,
                max(512, pressure_history_allocation(turn, fixed)),
            ),
        )
    except KernelError as error:
        if error.code not in {
            "context_compaction_retained_overflow",
            "context_compaction_no_progress",
            "context_compaction_source_overflow",
        }:
            raise
        return None, None
    instructions = pressure_summary_instructions(summary_instructions, host._compaction, planned)
    summary_cost = estimate_model_input_tokens((summary_request_item(planned),), (), instructions)
    summary_cost += summary_output_limit(host._compaction, planned)
    savings = (
        planned.plan.source_history_tokens
        - planned.plan.retained_history_tokens
        - planned.plan.policy.summary_reserve_tokens
    )
    # 同一保守估算尺度判断摊销价值，不把微小窗口缩减当成值得额外付费的压缩。
    future_requests = min(3, require_request_slots(turn)) - 1
    if summary_cost > savings * future_requests:
        return None, None
    cost = summary_cost
    cost += (
        planned.plan.retained_history_tokens + planned.plan.policy.summary_reserve_tokens + fixed
    )
    cost += closing_reserve(turn)
    if (
        cost > turn.budget.max_tokens - turn.usage.total_tokens
        or summary_output_limit(host._compaction, planned) == 0
        or len(planned.summary_source) > 1_000_000
    ):
        return None, None
    return planned, context


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
        planned = None
        required = _requires_compaction(host, prepared, reactive_compaction_required)
        if not required and host._compaction is not None and host._budget_pressure_compaction:
            planned, budget_context = await _budget_plan(
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
            )
