"""Kernel 专用 Patch 准入与结算；不持有存储、文件或后台执行所有权。"""

from typing import Literal

from harnessix.agent.approvals import (
    approval_for,
    approval_matches,
    remaining_seconds,
    request_fingerprint,
)
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    ItemStatus,
    PatchApprovalRequestContent,
    PatchEffect,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.patches.agent_bridge import PatchCallResult


def inspection_scope(thread: Thread, turn: Turn, call: ToolCallContent) -> ToolExecutionScope:
    from harnessix.agent.reducer import pending_calls

    if thread.active_turn_id != turn.turn_id or call not in pending_calls(turn):
        raise KernelError("tool_scope_mismatch", "核对对象不属于当前未结算调用")
    # 刻意不放宽 for_pending_call；此上下文只能用于宿主核对，不授予执行许可。
    return ToolExecutionScope(
        thread.thread_id,
        turn.turn_id,
        call.call_id,
        thread.workspace,
        request_fingerprint(thread, turn, call),
    )


def execution_approval(
    thread: Thread, turn: Turn, call: ToolCallContent
) -> PatchApprovalRequestContent:
    from harnessix.agent.reducer import pending_calls

    item = approval_for(turn, call)
    if (
        thread.active_turn_id != turn.turn_id
        or turn.status != TurnStatus.EXECUTING_TOOLS
        or not pending_calls(turn)
        or pending_calls(turn)[0] != call
        or remaining_seconds(turn) <= 0
        or item is None
        or item.status != ItemStatus.COMPLETED
        or not isinstance(item.content, PatchApprovalRequestContent)
        or item.content.decision is None
        or not approval_matches(thread, turn, call, item.content)
    ):
        raise KernelError("patch_execution_not_authorized", "Patch 尚未取得当前调用的持久执行准入")
    return item.content


def result_content(
    result: PatchCallResult, call: ToolCallContent, origin: Literal["execution", "recovery"]
) -> ToolResultContent:
    if result.result.call_id != call.call_id or result.result.patch is not None:
        raise KernelError("tool_result_mismatch", "桥接结果归属错误或预置了 Kernel 证据")
    plan, record = result.plan, result.record
    effect = None
    if plan is not None and record is not None:
        if (
            plan.plan_id,
            plan.workspace_id,
            plan.request_id,
            plan.backend_fingerprint,
            plan.manifest,
        ) != (
            record.plan_id,
            record.workspace_id,
            record.request_id,
            record.approval_fingerprint,
            record.manifest,
        ):
            raise KernelError("patch_result_mismatch", "桥接计划与账本证据不一致")
        effect = PatchEffect(
            workspace_id=plan.workspace_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            approval_fingerprint=plan.approval_fingerprint,
            state=record.state,
            origin=origin,
        )
    if result.result.outcome == "succeeded" and effect is None:
        raise KernelError("patch_result_mismatch", "成功 Patch 缺少持久证据")
    return result.result.model_copy(update={"patch": effect})
