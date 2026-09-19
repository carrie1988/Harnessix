"""Agent重开与终结路径中的副作用核对；禁止通过本模块启动新写入。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from harnessix.agent import batch_patching
from harnessix.agent.approvals import approval_for, approval_matches
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    ItemStatus,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    QuestionAnswerContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.patching import inspection_scope, result_content
from harnessix.agent.ports import PatchBatchRuntime, PatchRuntime
from harnessix.agent.reducer_support import pending_calls
from harnessix.agent.trusted_action_runtime import (
    TrustedActionSessionRuntime,
    action_owned,
    recover_trusted_action,
)
from harnessix.context.compaction_ledger_contracts import CompactionRecord
from harnessix.domain.models import EffectClass

type ToolContractValidator = Callable[[ToolCallContent], None]
type TurnStateTransition = Callable[[UUID, UUID, TurnStatus], Awaitable[Thread]]


def result_resume_safe(turn: Turn) -> bool:
    """仅允许已持久回答或Trusted Action终态结果继续模型循环。"""

    answers = [
        item.content
        for item in turn.items
        if item.status == ItemStatus.COMPLETED and isinstance(item.content, QuestionAnswerContent)
    ]
    question_safe = bool(answers) and any(
        item.status == ItemStatus.COMPLETED
        and isinstance(item.content, ToolResultContent)
        and item.content.call_id == answers[-1].call_id
        and item.content.outcome == "succeeded"
        for item in turn.items
    )
    trusted_safe = not pending_calls(turn) and any(
        item.status == ItemStatus.COMPLETED
        and isinstance(item.content, ToolResultContent)
        and item.content.trusted_action is not None
        for item in turn.items
    )
    return question_safe or trusted_safe


async def consume_decided_approval(
    thread: Thread,
    turn: Turn,
    transition: TurnStateTransition,
    fault: Callable[[str], None],
) -> tuple[Thread, Turn]:
    """持久消费审批等待边界；恢复只接受已处于工具执行状态。"""

    if turn.status is TurnStatus.WAITING_APPROVAL:
        thread = await transition(thread.thread_id, turn.turn_id, TurnStatus.EXECUTING_TOOLS)
        turn = next(item for item in thread.turns if item.turn_id == turn.turn_id)
        fault("runtime.after_approval_consumed")
    elif turn.status is not TurnStatus.EXECUTING_TOOLS:
        raise KernelError("approval_projection_mismatch", "已决定审批不处于等待或恢复执行状态")
    return thread, turn


def recoverable_compaction(turn: Turn, event_sequence: int) -> CompactionRecord | None:
    """返回恰好在当前Session序号完成摘要、尚待激活的最近记录。"""

    return next(
        (
            record
            for record in reversed(turn.compactions)
            if record.status == "summarized" and record.finished_event_sequence == event_sequence
        ),
        None,
    )


async def recover_pending_effects(
    thread: Thread,
    turn: Turn,
    *,
    patches: PatchRuntime | None,
    patch_batches: PatchBatchRuntime | None,
    trusted_actions: TrustedActionSessionRuntime | None,
    validate_tool_contract: ToolContractValidator,
) -> dict[UUID, ToolResultContent]:
    """只核对尚无结果的副作用调用，返回可安全追加的有界投影。"""

    recorded = {
        item.content.call_id for item in turn.items if isinstance(item.content, ToolResultContent)
    }
    recovered: dict[UUID, ToolResultContent] = {}
    for call in pending_calls(turn):
        if call.call_id in recorded:
            continue
        result = await _recover_effect(
            thread,
            turn,
            call,
            patches=patches,
            patch_batches=patch_batches,
            trusted_actions=trusted_actions,
            validate_tool_contract=validate_tool_contract,
        )
        if result is not None:
            recovered[call.call_id] = result
    return recovered


async def _recover_effect(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    *,
    patches: PatchRuntime | None,
    patch_batches: PatchBatchRuntime | None,
    trusted_actions: TrustedActionSessionRuntime | None,
    validate_tool_contract: ToolContractValidator,
) -> ToolResultContent | None:
    if action_owned(trusted_actions, call):
        return await recover_trusted_action(trusted_actions, thread, turn, call)
    if call.effect_class is not EffectClass.NON_IDEMPOTENT_WRITE:
        return None
    if call.tool == "apply_patch":
        return await _recover_patch(thread, turn, call, patches, validate_tool_contract)
    if call.tool == "apply_patch_batch":
        return await _recover_patch_batch(thread, turn, call, patch_batches, validate_tool_contract)
    return None


async def _recover_patch(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    patches: PatchRuntime | None,
    validate_tool_contract: ToolContractValidator,
) -> ToolResultContent:
    try:
        if patches is None:
            raise KernelError("patch_not_enabled", "原 Patch 核对入口不可用")
        validate_tool_contract(call)
        item = approval_for(turn, call)
        content = item.content if item is not None else None
        if content is not None and not isinstance(content, PatchApprovalRequestContent):
            raise KernelError("approval_mismatch", "写调用不匹配写审批")
        if content is not None and not approval_matches(thread, turn, call, content):
            raise KernelError("approval_mismatch", "写调用与持久计划不一致")
        settled = await patches.recover(
            call,
            inspection_scope(thread, turn, call),
            CancelToken(),
            plan=content.plan if content else None,
            approval=content.decision if content else None,
        )
        result = result_content(settled, call, "recovery")
        if (
            len(result.model_dump_json(exclude={"patch", "patch_batch"}))
            > turn.budget.max_output_chars
        ):
            result = result.model_copy(update={"output": None})
        return result
    except Exception as error:
        return _recovery_failure(call, error, "patch_recovery_failed", "Patch 核对失败")


async def _recover_patch_batch(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    patch_batches: PatchBatchRuntime | None,
    validate_tool_contract: ToolContractValidator,
) -> ToolResultContent:
    try:
        if patch_batches is None:
            raise KernelError("patch_batch_not_enabled", "原整组核对端口不可用")
        validate_tool_contract(call)
        item = approval_for(turn, call)
        content = item.content if item is not None else None
        if content is not None and (
            not isinstance(content, PatchBatchApprovalRequestContent)
            or not approval_matches(thread, turn, call, content)
        ):
            raise KernelError("approval_mismatch", "整组调用与持久计划不一致")
        assert content is None or isinstance(content, PatchBatchApprovalRequestContent)
        settled = await patch_batches.recover(
            call,
            inspection_scope(thread, turn, call),
            CancelToken(),
            plan=content.plan if content else None,
            approval=content.decision if content else None,
        )
        result = batch_patching.result_content(settled, thread, turn, call, "recovery")
        if (
            len(result.model_dump_json(exclude={"patch", "patch_batch"}))
            > turn.budget.max_output_chars
        ):
            result = result.model_copy(update={"output": None})
        return result
    except Exception as error:
        return _recovery_failure(call, error, "patch_batch_recovery_failed", "整组核对失败")


def _recovery_failure(
    call: ToolCallContent, error: Exception, code: str, message: str
) -> ToolResultContent:
    failure = (
        error.to_failure()
        if isinstance(error, KernelError)
        else AgentFailure(code=code, message=f"{message}；原始错误未持久化")
    )
    return ToolResultContent(call_id=call.call_id, outcome="unknown", error=failure)
