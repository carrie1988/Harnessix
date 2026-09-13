"""模型历史运行边界：在可并发Steering下验证并原子冻结下一步模型输入。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.models import (
    EventDraft,
    ModelHistoryPrepared,
    Thread,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import get_turn
from harnessix.artifacts.ports import BatchDiffPublisher
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import PreparedModelHistory, history_document
from harnessix.session.ports import SessionStore


@dataclass(frozen=True, slots=True)
class PreparedHistoryStep:
    thread: Thread
    prepared: PreparedModelHistory
    reactive_compaction_required: bool


class ModelHistoryHost(Protocol):
    store: SessionStore
    _batch_diffs: BatchDiffPublisher | None
    _tool_result_view_policy: ToolResultViewPolicy
    _compaction: CompactionRuntimeConfig | None
    _fault: Callable[[str], None]

    def _lock(self, thread_id: UUID) -> AbstractAsyncContextManager[None]: ...

    async def _run_compaction(
        self,
        thread: Thread,
        turn: Turn,
        prepared: PreparedModelHistory,
        token: CancelToken,
    ) -> Thread: ...

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
) -> PreparedHistoryStep:
    """验证后若Session发生变化则重备，直到同一快照完成原子提交。"""

    while True:
        token.checkpoint()
        thread = await host.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        prepared = prepare_active_model_history(thread, model_step, host._tool_result_view_policy)
        if _requires_compaction(host, prepared, reactive_compaction_required):
            thread = await host._run_compaction(thread, turn, prepared, token)
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
            )
