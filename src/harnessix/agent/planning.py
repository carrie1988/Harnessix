"""内置规划控制：原子发布模型自报计划和配对结果，不授予工作区执行权限。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
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
from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.session.ports import SessionStore

UPDATE_PLAN_TOOL = "update_plan"
UPDATE_PLAN_VERSION = "harnessix.update-plan/v1"
MAX_PLAN_INPUT_BYTES = 4096
_PLAN_ITEM_NAMESPACE = "harnessix.plan-item/v1"
# 模型常只发单个步骤（如{"step_id": "verify", "status": "completed"}）或漏写description；
# 形状与字段错误附一份完整合法调用，说明每次都发送整份计划，而不是只发变化的步骤。
_WHOLE_PLAN_EXAMPLE = (
    "Send the whole plan (every step) on each call, not only the step that changed. Example: "
    '{"steps": [{"step_id": "reproduce", "description": "Reproduce the bug", '
    '"status": "completed"}, {"step_id": "fix", "description": "Fix the root cause", '
    '"status": "in_progress"}]}'
)
# 调用组发布时计划调用已排到末尾（plan_calls_last），其后仍有未结算调用只会是同一响应里更晚的
# 计划调用：每次调用都是整份计划，只有最后一份随准备阶段原子发布。
_SUPERSEDED_PLAN = (
    "This plan was not recorded: the same response contains a later update_plan call, and only "
    "the last one is recorded. Send update_plan once per response."
)
_Proposal = ToolCallCompleted | ToolCallRejected


class UpdatePlanInput(ContractModel):
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=8)


def planning_tool_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name=UPDATE_PLAN_TOOL,
        version=UPDATE_PLAN_VERSION,
        description=(
            "Record or revise a short work plan of at most 8 steps; the argument JSON is "
            "limited to 4096 UTF-8 bytes. Send the whole plan on every call. It may share a "
            "response with other tool calls, in any position; do not spend a request only to "
            "restate the plan. Step IDs are unique and at most one step is in_progress. "
            "completed is self-reported: it does not prove an edit, test or review and grants "
            "no permission."
        ),
        input_schema=UpdatePlanInput.model_json_schema(),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        requires_idempotency=False,
        requires_approval=False,
        supports_reconciliation=False,
        supports_parallel_calls=False,
    )


def _is_plan_call(proposal: _Proposal) -> bool:
    return isinstance(proposal, ToolCallCompleted) and proposal.tool == UPDATE_PLAN_TOOL


def plan_calls_last(proposals: Sequence[_Proposal], enabled: bool) -> list[_Proposal]:
    """返回持久调用组的顺序：其余调用保持模型给出的相对顺序，计划调用稳定移到其后。

    计划Item只能在整组调用结算后进入准备阶段时与其结果原子发布，而结果必须按调用顺序结算；
    因此模型可以把update_plan写在响应的任意位置，由宿主把它排到调用组末尾。计划只是自报记录，
    不读写工作区，后移不改变其他调用的输入与效果。未启用内置规划时同名工具不属于本模块，保持原序。
    """
    return sorted(proposals, key=_is_plan_call) if enabled else list(proposals)


class PlanningHost(Protocol):
    store: SessionStore
    _batch_diffs: BatchDiffPublisher | None
    _public_output_protection: PublicOutputProtection | None
    _fault: Callable[[str], None]

    def _lock(self, thread_id: UUID) -> AbstractAsyncContextManager[None]: ...

    def _validate_result(
        self, result: ToolResultContent, call: ToolCallContent, max_chars: int
    ) -> ToolResultContent: ...


def _with_step_ids(arguments: dict[str, Any]) -> dict[str, Any]:
    """模型常省略step_id（评测中被拒计划的主因）：按位置补齐，不改动已给出的ID、不制造重复。"""
    steps = arguments.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        return arguments
    taken = {step["step_id"] for step in steps if isinstance(step.get("step_id"), str)}
    filled = []
    for index, step in enumerate(steps, start=1):
        if "step_id" not in step:
            candidate = str(index)
            while candidate in taken:
                candidate = f"step-{candidate}"
            taken.add(candidate)
            step = {"step_id": candidate, **step}
        filled.append(step)
    return {**arguments, "steps": filled}


def _plan_rule_violation(arguments: dict[str, Any], body: bytes) -> str:
    """说明违反的具体规则，模型据此修正而不是原样重发。"""
    if len(body) > MAX_PLAN_INPUT_BYTES:
        return (
            f"The argument JSON is {len(body)} bytes; the limit is {MAX_PLAN_INPUT_BYTES} bytes. "
            "Shorten the step descriptions."
        )
    steps = arguments.get("steps")
    if not isinstance(steps, list):
        return (
            'Arguments must be {"steps": [{"step_id", "description", "status"}, ...]}. '
            + _WHOLE_PLAN_EXAMPLE
        )
    if not 1 <= len(steps) <= 8:
        return f"A plan needs 1 to 8 steps; this one has {len(steps)}."
    ids = [step.get("step_id") for step in steps if isinstance(step, dict)]
    if len(set(ids)) != len(ids):
        return "step_id values repeat; give every step a different step_id."
    active = sum(isinstance(step, dict) and step.get("status") == "in_progress" for step in steps)
    if active > 1:
        return f"At most one step may be in_progress; this plan has {active}."
    return (
        "Each step takes only step_id, description (non-empty) and status "
        "(pending, in_progress or completed). " + _WHOLE_PLAN_EXAMPLE
    )


def _decode_plan(call: ToolCallContent) -> tuple[UpdatePlanInput | None, str]:
    """返回解析结果；失败时附具体原因。"""
    arguments = _with_step_ids(call.arguments)
    try:
        body = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError):
        return None, 'Arguments must be a JSON object {"steps": [...]}.'
    if len(body) > MAX_PLAN_INPUT_BYTES:
        return None, _plan_rule_violation(arguments, body)
    try:
        parsed = UpdatePlanInput.model_validate_json(body)
        # 复用领域校验；客户端不能提供计划身份或绕过版本链。
        PlanContent(steps=parsed.steps)
    except (TypeError, ValueError, UnicodeError):
        return None, _plan_rule_violation(arguments, body)
    if any("\x00" in step.step_id + step.description for step in parsed.steps):
        return None, "step_id and description must not contain NUL characters."
    return parsed, ""


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
    parsed, violation = _decode_plan(call)
    async with host._lock(thread_id):
        thread = await host.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        _require_active_call(turn, call, cancel)
        plan_id = uuid5(call.call_id, _PLAN_ITEM_NAMESPACE)
        others = len(pending_calls(turn)) - 1
        if parsed is None or others:
            result = ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                error=AgentFailure(
                    code="tool_invalid_arguments", message=violation or _SUPERSEDED_PLAN
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
        parsed, _ = _decode_plan(call)
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
