"""内置规划控制：原子发布模型自报计划和配对结果，不授予工作区执行权限。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import Field

from harnessix.agent.approvals import remaining_seconds, tool_fingerprint
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    EventDraft,
    EventPayload,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PlanContent,
    PlanStep,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.publication import PublicOutputProtection, protect_json
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.artifacts.ports import BatchDiffPublisher
from harnessix.context.compaction_contracts import CompactionAnchor
from harnessix.context.tool_result_view import history_document, history_items
from harnessix.domain.models import ContractModel, EffectClass, RiskLevel, ToolDescriptor
from harnessix.session.ports import SessionStore

UPDATE_PLAN_TOOL = "update_plan"
UPDATE_PLAN_VERSION = "harnessix.update-plan/v1"
MAX_PLAN_INPUT_BYTES = 4096
_PLAN_ITEM_NAMESPACE = "harnessix.plan-item/v1"


class UpdatePlanInput(ContractModel):
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=8)


def planning_tool_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name=UPDATE_PLAN_TOOL,
        version=UPDATE_PLAN_VERSION,
        description=(
            "记录或修订最多8步的简短工作计划；参数JSON共不超过4096 UTF-8字节。"
            "必须放在本次响应的所有其他工具之后，不单独花费请求重复计划。"
            "步骤ID唯一，最多一个in_progress；completed仅为模型自报，"
            "不证明修改、测试或Review完成，也不授予执行权限。"
        ),
        input_schema=UpdatePlanInput.model_json_schema(),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        requires_idempotency=False,
        requires_approval=False,
        supports_reconciliation=False,
        supports_parallel_calls=False,
    )


class PlanningHost(Protocol):
    store: SessionStore
    _batch_diffs: BatchDiffPublisher | None
    _public_output_protection: PublicOutputProtection | None
    _fault: Callable[[str], None]

    def _lock(self, thread_id: UUID) -> AbstractAsyncContextManager[None]: ...

    def _validate_result(
        self, result: ToolResultContent, call: ToolCallContent, max_chars: int
    ) -> ToolResultContent: ...


def _decode_plan(call: ToolCallContent) -> UpdatePlanInput | None:
    try:
        body = json.dumps(call.arguments, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > MAX_PLAN_INPUT_BYTES:
            return None
        parsed = UpdatePlanInput.model_validate_json(body)
        # 复用领域校验；客户端不能提供计划身份或绕过版本链。
        PlanContent(steps=parsed.steps)
        if any("\x00" in step.step_id + step.description for step in parsed.steps):
            return None
        return parsed
    except (TypeError, ValueError, UnicodeError):
        return None


def _require_active_call(turn: Turn, call: ToolCallContent, cancel: CancelToken) -> None:
    cancel.checkpoint()
    if turn.status in {TurnStatus.CANCELLING, TurnStatus.CANCELLED}:
        raise TurnCancelled
    if turn.status != TurnStatus.EXECUTING_TOOLS or call not in pending_calls(turn):
        raise KernelError("tool_call_not_pending", "计划调用不属于当前待结算工具组")
    if remaining_seconds(turn) <= 0:
        raise KernelError("time_budget_exceeded", "原Turn期限已到，不发布计划")
    if turn.usage.total_tokens >= turn.budget.max_tokens:
        raise KernelError("budget_exceeded", "原Turn Token已耗尽，不发布计划")


def _latest_plan_id(thread: Thread) -> UUID | None:
    return next(
        (
            item.item_id
            for turn in reversed(thread.turns)
            for item in reversed(turn.items)
            if item.status == ItemStatus.COMPLETED and isinstance(item.content, PlanContent)
        ),
        None,
    )


def _completed_item(item_id: UUID, content: ToolResultContent | PlanContent) -> list[EventPayload]:
    return [
        ItemStarted(item_id=item_id, content=content),
        ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
    ]


async def execute_plan_update(
    host: PlanningHost, thread_id: UUID, turn_id: UUID, call: ToolCallContent, cancel: CancelToken
) -> None:
    """同一事务结算调用、进入准备阶段并追加计划；失败反馈不产生计划。"""
    parsed = _decode_plan(call)
    async with host._lock(thread_id):
        thread = await host.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        _require_active_call(turn, call, cancel)
        plan_id = uuid5(call.call_id, _PLAN_ITEM_NAMESPACE)
        if parsed is None or len(pending_calls(turn)) != 1:
            result = ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                error=AgentFailure(
                    code="tool_invalid_arguments",
                    message="计划需1到8个唯一步骤、最多一个进行中、JSON不超过4096字节且位于工具组末尾",
                ),
            )
        else:
            result = ToolResultContent(
                call_id=call.call_id,
                outcome="succeeded",
                output={
                    "plan_item_id": str(plan_id),
                    "step_count": len(parsed.steps),
                    "self_reported": True,
                },
            )
        checked = host._validate_result(result, call, turn.budget.max_output_chars)
        payloads = _completed_item(uuid5(call.call_id, "harnessix.plan-result/v1"), checked)
        if checked.outcome == "succeeded":
            assert parsed is not None
            plan = PlanContent(steps=parsed.steps, supersedes=_latest_plan_id(thread))
            if len(plan.model_dump_json()) > turn.budget.max_output_chars:
                raise KernelError("tool_output_too_large", "计划输出超过当前 Kernel 上限")
            payloads.extend(
                [
                    TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
                    *_completed_item(plan_id, plan),
                ]
            )
        # 扫描实际发布批次，包括失败结果；保护后仍须在原锁内复核取消和期限。
        await protect_json(
            host._public_output_protection,
            [payload.model_dump(mode="json") for payload in payloads],
            cancel,
        )
        host._fault("runtime.before_plan_update_commit")
        _require_active_call(turn, call, cancel)
        await (host._batch_diffs or host.store).append(
            thread_id,
            [EventDraft(turn_id=turn_id, payload=payload) for payload in payloads],
            expected_sequence=thread.sequence,
        )
    host._fault("runtime.after_plan_update_commit")


def planning_history_anchors(thread: Thread) -> tuple[CompactionAnchor, ...]:
    """只固定与最新原生计划同身份的工具组；人工旧Plan不改变历史压缩。"""
    plan_id = _latest_plan_id(thread)
    if plan_id is None:
        return ()
    latest_plan = next(
        item.content
        for turn in thread.turns
        for item in turn.items
        if item.item_id == plan_id and isinstance(item.content, PlanContent)
    )
    history = history_items(thread)
    results = {
        item.content.call_id: item.content
        for item in history
        if isinstance(item.content, ToolResultContent)
    }
    descriptor = planning_tool_descriptor()
    for item in reversed(history):
        call = item.content
        if not isinstance(call, ToolCallContent) or call.tool != UPDATE_PLAN_TOOL:
            continue
        result = results.get(call.call_id)
        parsed = _decode_plan(call)
        if (
            call.tool_version == UPDATE_PLAN_VERSION
            and call.tool_fingerprint == tool_fingerprint(descriptor)
            and plan_id == uuid5(call.call_id, _PLAN_ITEM_NAMESPACE)
            and parsed is not None
            and parsed.steps == latest_plan.steps
            and result is not None
            and result.outcome == "succeeded"
            and isinstance(result.output, dict)
            and result.output
            == {
                "plan_item_id": str(plan_id),
                "step_count": len(parsed.steps),
                "self_reported": True,
            }
        ):
            return (
                CompactionAnchor(
                    item_id=item.item_id,
                    source_sha256=hashlib.sha256(history_document(item).encode()).hexdigest(),
                ),
            )
    return ()
