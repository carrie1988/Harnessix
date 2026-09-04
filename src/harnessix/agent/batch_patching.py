"""Kernel 整组授权和效果投影；不持有文件、后台任务或存储连接。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from harnessix.agent.approvals import approval_for, approval_matches, remaining_seconds
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    PatchBatchApprovalRequestContent,
    PatchBatchEffect,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchOutput

if TYPE_CHECKING:
    from harnessix.patches.batch_agent_bridge import BatchCallResult


def execution_approval(
    thread: Thread, turn: Turn, call: ToolCallContent
) -> PatchBatchApprovalRequestContent:
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
        or not isinstance(item.content, PatchBatchApprovalRequestContent)
        or item.content.decision is None
        or not approval_matches(thread, turn, call, item.content)
    ):
        raise KernelError("patch_batch_execution_not_authorized", "整组调用尚未取得持久执行准入")
    return item.content


def validate_effect(
    thread: Thread, turn: Turn, call: ToolCallContent, content: ToolResultContent
) -> None:
    from harnessix.agent.reducer import require

    effect = content.patch_batch
    assert effect is not None
    try:
        effect = PatchBatchEffect.model_validate_json(effect.model_dump_json())
    except ValidationError:
        raise KernelError("invalid_event", "整组私有效果契约损坏") from None
    require(content.patch is None, "整组证据不能混入单文件效果")
    item = approval_for(turn, call)
    require(
        item is not None
        and item.status == ItemStatus.COMPLETED
        and isinstance(item.content, PatchBatchApprovalRequestContent)
        and item.content.decision is not None
        and approval_matches(thread, turn, call, item.content),
        "整组效果缺少当前调用的完整持久批准",
    )
    assert item is not None and isinstance(item.content, PatchBatchApprovalRequestContent)
    request = item.content
    plan, backend = request.plan, request.plan.backend
    require(
        (effect.workspace_id, effect.batch_id, effect.request_id, effect.approval_fingerprint)
        == (backend.workspace_id, backend.batch_id, backend.request_id, plan.approval_fingerprint),
        "整组效果与调用计划错绑",
    )
    require(
        effect.origin == "recovery" or turn.status == TurnStatus.EXECUTING_TOOLS,
        "执行效果只能在执行状态发布",
    )
    execution = effect.execution
    if execution is not None:
        require(
            execution.run.phase == "finished"
            and execution.run.workspace_id == backend.workspace_id
            and execution.run.batch_id == backend.batch_id
            and execution.run.approval_fingerprint == backend.approval_fingerprint
            and tuple(m.plan_id for m in execution.members)
            == tuple(m.plan_id for m in backend.members),
            "整组运行或有序成员与完整批准不一致",
        )
        require(
            request.decision is not None and request.decision.outcome == ApprovalOutcome.APPROVED,
            "未批准组不能带执行事实",
        )
        if effect.origin == "execution":
            require(
                execution.run.stop_reason != "interrupted"
                and all(
                    m.state in {"pending", "approved", "started", "applied", "failed", "uncertain"}
                    for m in execution.members
                ),
                "执行不能伪造恢复观察",
            )
    aggregate = execution.effect if execution else "not_applied"
    expected = (
        "succeeded" if aggregate == "applied" else "unknown" if aggregate == "unknown" else "failed"
    )
    require(content.outcome == expected, "整组结果与私有效果不一致")
    if content.output is not None:
        try:
            output = ManagedPatchBatchOutput.model_validate_json(
                json.dumps(content.output, allow_nan=False)
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("invalid_event", "整组公开输出不符合契约") from None
        require(
            output.effect == aggregate
            and output.phase == (execution.run.phase if execution else "not_started")
            and output.stop_reason == (execution.run.stop_reason if execution else None)
            and len(output.files) == len(backend.members),
            "整组公开摘要与私有证据不一致",
        )
        for index, (file, manifest) in enumerate(
            zip(output.files, backend.manifest.files, strict=True)
        ):
            require(
                (file.path, file.before_sha256, file.after_sha256, file.state)
                == (
                    manifest.path,
                    manifest.before_sha256,
                    manifest.after_sha256,
                    execution.members[index].state if execution else "pending",
                ),
                "整组公开成员与完整计划不一致",
            )


def result_content(
    result: BatchCallResult,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    origin: Literal["execution", "recovery"],
) -> ToolResultContent:
    if (
        result.result.call_id != call.call_id
        or result.result.patch is not None
        or result.result.patch_batch is not None
        or result.result.diff_artifact is not None
    ):
        raise KernelError("tool_result_mismatch", "整组结果归属错误或预置了私有效果")
    plan, approval, execution = result.plan, result.approval, result.execution
    if plan is None or approval is None:
        if (
            plan is not None
            or approval is not None
            or execution is not None
            or result.result.outcome != "unknown"
        ):
            raise KernelError("patch_batch_result_mismatch", "整组结果缺少完整证据")
        return result.result
    item = approval_for(turn, call)
    if (
        item is None
        or not isinstance(item.content, PatchBatchApprovalRequestContent)
        or item.content.plan != plan
        or item.content.decision is None
        or plan.backend != approval.plan
        or ApprovalDecision.model_validate_json(
            item.content.decision.model_dump_json(include={"outcome", "actor", "reason"})
        )
        != approval.decision
    ):
        raise KernelError("patch_batch_result_mismatch", "桥接计划或决定与持久审批不一致")
    effect = PatchBatchEffect(
        workspace_id=plan.backend.workspace_id,
        batch_id=plan.backend.batch_id,
        request_id=plan.backend.request_id,
        approval_fingerprint=plan.approval_fingerprint,
        origin=origin,
        execution=execution,
    )
    content = result.result.model_copy(update={"patch_batch": effect})
    # 再校验可绕过模型构造的宿主返回值，在线与 Replay 共用边界。
    content = ToolResultContent.model_validate_json(content.model_dump_json())
    validate_effect(thread, turn, call, content)
    return content
