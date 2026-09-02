from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from uuid import UUID

from harnessix.agent.approvals import approval_for, request_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    TURN_TRANSITIONS,
    AgentEvent,
    ApprovalRequestContent,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    Thread,
    ThreadCreated,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.domain.models import ApprovalOutcome, EffectClass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise KernelError("invalid_event", message)


def get_turn(thread: Thread, turn_id: UUID) -> Turn:
    for turn in thread.turns:
        if turn.turn_id == turn_id:
            return turn
    raise KernelError("turn_not_found", "Turn 不存在")


def pending_calls(turn: Turn) -> list[ToolCallContent]:
    settled = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, ToolResultContent) and item.status != ItemStatus.STARTED
    }
    return [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
        and item.status == ItemStatus.COMPLETED
        and item.content.call_id not in settled
    ]


def _start_item(thread: Thread, turn: Turn, payload: ItemStarted) -> Turn:
    require(all(i.item_id != payload.item_id for i in turn.items), "Item ID 已存在")
    content = payload.content
    if isinstance(content, TextContent):
        if content.kind == "user_message":
            require(turn.status == TurnStatus.ACCEPTED, "用户输入只能在 Turn 接受时记录")
            require(not turn.items, "同一 Turn 只能接受一份用户输入")
        else:
            require(turn.status == TurnStatus.CALLING_MODEL, "模型 Item 只能在模型步骤内开始")
    elif isinstance(content, ToolCallContent):
        require(turn.status == TurnStatus.CALLING_MODEL, "Tool Call 只能由模型步骤产生")
        require(
            all(
                not isinstance(i.content, ToolCallContent) or i.content.call_id != content.call_id
                for i in turn.items
            ),
            "Tool Call ID 重复",
        )
    elif isinstance(content, ApprovalRequestContent):
        calls = pending_calls(turn)
        require(turn.status == TurnStatus.EXECUTING_TOOLS, "审批请求只能在执行边界生成")
        require(bool(calls) and calls[0].call_id == content.call_id, "审批与当前调用不匹配")
        call = calls[0]
        require(
            call.requires_approval and call.effect_class == EffectClass.READ_ONLY,
            "当前只支持可信只读工具审批",
        )
        require(call.tool_fingerprint is not None, "审批缺少工具契约指纹")
        require(approval_for(turn, call) is None, "调用已存在审批请求")
        require(
            all(
                not isinstance(i.content, ApprovalRequestContent)
                or i.content.approval_id != content.approval_id
                for i in turn.items
            ),
            "审批 ID 重复",
        )
        require(content.decision is None, "审批请求不能预置决定")
        require(
            content.request_fingerprint
            == request_fingerprint(thread, turn, call, policy_version=content.policy_version),
            "审批指纹不匹配",
        )
    else:
        calls = pending_calls(turn)
        require(bool(calls) and calls[0].call_id == content.call_id, "Tool Result 缺失、重复或乱序")
        require(
            not any(
                isinstance(i.content, ToolResultContent) and i.content.call_id == content.call_id
                for i in turn.items
            ),
            "Tool Result 已开始",
        )
        if content.outcome == "succeeded":
            require(turn.status == TurnStatus.EXECUTING_TOOLS, "执行阶段之外不能记录成功结果")
            if calls[0].requires_approval:
                approval = approval_for(turn, calls[0])
                require(
                    approval is not None and approval.status == ItemStatus.COMPLETED,
                    "成功结果之前必须持久记录审批决定",
                )
                assert approval is not None and isinstance(approval.content, ApprovalRequestContent)
                require(
                    approval.content.decision is not None
                    and approval.content.decision.outcome == ApprovalOutcome.APPROVED,
                    "未经批准的调用不能成功",
                )
    item = Item(item_id=payload.item_id, status=ItemStatus.STARTED, content=content)
    return turn.model_copy(update={"items": (*turn.items, item)})


def _finish_item(turn: Turn, event: AgentEvent, payload: ItemFinished) -> Turn:
    original = next((i for i in turn.items if i.item_id == payload.item_id), None)
    require(original is not None, "Item 必须先开始")
    assert original is not None
    require(original.status == ItemStatus.STARTED, "Item 终态不可改写")
    require(original.content.kind == payload.content.kind, "Item 类型不可改变")
    if isinstance(original.content, ApprovalRequestContent):
        require(isinstance(payload.content, ApprovalRequestContent), "审批 Item 类型不可改变")
        assert isinstance(payload.content, ApprovalRequestContent)
        require(
            original.content == payload.content.model_copy(update={"decision": None}),
            "审批请求身份与指纹不可变",
        )
        decision = payload.content.decision
        if payload.status == ItemStatus.COMPLETED:
            require(turn.status == TurnStatus.WAITING_APPROVAL, "审批答复只能在等待状态提交")
            require(decision is not None, "审批答复缺少决定")
            assert decision is not None
            require(
                decision.request_fingerprint == original.content.request_fingerprint,
                "审批决定指纹不匹配",
            )
            require(decision.decided_at == event.occurred_at, "审批决定时间必须与事件一致")
            require(
                turn.created_at
                <= event.occurred_at
                < turn.created_at + timedelta(seconds=turn.budget.timeout_seconds),
                "审批答复超过 Turn 时间预算",
            )
        else:
            require(decision is None, "取消或失败不能伪造审批决定")
    elif not isinstance(original.content, TextContent):
        require(original.content == payload.content, "Tool Call/Result 身份与内容不可变")
    finished = Item(
        item_id=payload.item_id,
        status=payload.status,
        content=payload.content,
        error=payload.error,
    )
    return turn.model_copy(
        update={
            "items": tuple(finished if i.item_id == finished.item_id else i for i in turn.items)
        }
    )


def _change_state(turn: Turn, event: AgentEvent, payload: TurnStateChanged) -> Turn:
    target = payload.status
    allowed = set(TURN_TRANSITIONS.get(turn.status, set()))
    if turn.status != TurnStatus.CANCELLING:
        allowed.update({TurnStatus.CANCELLING, TurnStatus.FAILED, TurnStatus.INTERRUPTED})
    require(target in allowed, f"非法 Turn 状态转换：{turn.status} → {target}")
    if target in TERMINAL_TURNS:
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "存在未结算 Item")
        require(not pending_calls(turn), "存在未配对 Tool Call")
        if target == TurnStatus.COMPLETED:
            require(payload.error is None, "成功终态不能携带错误")
        if target in {TurnStatus.COMPLETED, TurnStatus.CANCELLED}:
            require(
                not any(
                    isinstance(i.content, ToolResultContent) and i.content.outcome == "unknown"
                    for i in turn.items
                ),
                "未知效果不能标记为完成或已取消",
            )
        if target != TurnStatus.COMPLETED:
            require(payload.error is not None, "非成功终态必须携带结构化错误")
        return turn.model_copy(
            update={"status": target, "error": payload.error, "completed_at": event.occurred_at}
        )
    if target in {TurnStatus.PREPARING_CONTEXT, TurnStatus.CALLING_MODEL}:
        require(
            any(
                isinstance(i.content, TextContent)
                and i.content.kind == "user_message"
                and i.status == ItemStatus.COMPLETED
                for i in turn.items
            ),
            "模型循环开始前必须持久接受用户输入",
        )
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "存在未结算 Item")
    if target in {TurnStatus.EXECUTING_TOOLS, TurnStatus.FINALIZING}:
        require(turn.usage_step == turn.model_steps, "模型响应未完整结束")
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "存在未结算 Item")
    if target == TurnStatus.WAITING_APPROVAL:
        calls = pending_calls(turn)
        require(bool(calls), "等待审批必须有未结算调用")
        approval = approval_for(turn, calls[0])
        require(
            approval is not None and approval.status == ItemStatus.STARTED,
            "等待审批必须先持久记录请求",
        )
        require(
            all(i.status != ItemStatus.STARTED or i == approval for i in turn.items),
            "等待审批时存在其他未完成 Item",
        )
    if turn.status == TurnStatus.WAITING_APPROVAL and target == TurnStatus.EXECUTING_TOOLS:
        calls = pending_calls(turn)
        require(bool(calls), "审批之后缺少当前调用")
        approval = approval_for(turn, calls[0])
        require(
            approval is not None and approval.status == ItemStatus.COMPLETED,
            "审批决定持久化前不能离开等待状态",
        )
        require(
            event.occurred_at < turn.created_at + timedelta(seconds=turn.budget.timeout_seconds),
            "Turn 时间预算已耗尽",
        )
    if target == TurnStatus.CALLING_MODEL:
        require(turn.model_steps < turn.budget.max_steps, "模型步骤预算耗尽")
        require(turn.usage.total_tokens < turn.budget.max_tokens, "Token 预算耗尽")
        require(not pending_calls(turn), "Tool Result 提交前不能开始下一模型步骤")
        return turn.model_copy(update={"status": target, "model_steps": turn.model_steps + 1})
    if target == TurnStatus.FINALIZING:
        require(not pending_calls(turn), "存在尚未执行的 Tool Call")
    return turn.model_copy(update={"status": target})


def apply_event(thread: Thread | None, event: AgentEvent) -> Thread:
    """唯一的状态投影器；在线提交和离线 Replay 使用相同校验。"""
    payload = event.payload
    if thread is None:
        require(event.sequence == 1, "首事件 sequence 必须为 1")
        require(isinstance(payload, ThreadCreated), "首事件必须创建 Thread")
        require(event.turn_id is None, "Thread 事件不能绑定 Turn")
        assert isinstance(payload, ThreadCreated)
        return Thread(
            thread_id=event.thread_id,
            workspace=payload.workspace,
            sequence=1,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
    require(thread.thread_id == event.thread_id, "事件属于其他 Thread")
    require(event.sequence == thread.sequence + 1, "事件序号存在缺口或倒序")
    require(not isinstance(payload, ThreadCreated), "Thread 不可重复创建")
    require(event.turn_id is not None, "Turn 事件缺少 turn_id")
    assert event.turn_id is not None
    if isinstance(payload, TurnStarted):
        require(thread.active_turn_id is None, "同一 Thread 已存在活跃 Turn")
        require(all(t.turn_id != event.turn_id for t in thread.turns), "Turn ID 已存在")
        require(
            all(t.request_id != payload.request_id for t in thread.turns),
            "request_id 已绑定其他 Turn",
        )
        turn = Turn(
            turn_id=event.turn_id,
            request_id=payload.request_id,
            request_fingerprint=payload.request_fingerprint,
            budget=payload.budget,
            trace_context=payload.trace_context,
            created_at=event.occurred_at,
        )
        turns = (*thread.turns, turn)
    else:
        require(thread.active_turn_id == event.turn_id, "不能修改非活跃 Turn")
        turn = get_turn(thread, event.turn_id)
        require(turn.status not in TERMINAL_TURNS, "终态不可重开")
        if isinstance(payload, TurnStateChanged):
            turn = _change_state(turn, event, payload)
        elif isinstance(payload, ItemStarted):
            require(
                all(i.item_id != payload.item_id for t in thread.turns for i in t.items),
                "Item ID 在 Thread 内重复",
            )
            turn = _start_item(thread, turn, payload)
        elif isinstance(payload, ItemFinished):
            turn = _finish_item(turn, event, payload)
        elif isinstance(payload, UsageRecorded):
            require(turn.status == TurnStatus.CALLING_MODEL, "用量只能在模型步骤内记录")
            require(payload.step == turn.model_steps, "用量不属于当前模型步骤")
            require(payload.step > turn.usage_step, "用量重复记账")
            turn = turn.model_copy(
                update={
                    "usage_step": payload.step,
                    "usage": Usage(
                        input_tokens=turn.usage.input_tokens + payload.usage.input_tokens,
                        output_tokens=turn.usage.output_tokens + payload.usage.output_tokens,
                    ),
                }
            )
        turns = tuple(turn if t.turn_id == turn.turn_id else t for t in thread.turns)
    return thread.model_copy(
        update={
            "turns": turns,
            "active_turn_id": None if turn.status in TERMINAL_TURNS else turn.turn_id,
            "sequence": event.sequence,
            "updated_at": event.occurred_at,
        },
        deep=True,
    )


def replay(events: Iterable[AgentEvent]) -> Thread:
    thread: Thread | None = None
    seen: set[UUID] = set()
    for event in events:
        require(event.event_id not in seen, "重放事件 ID 重复")
        seen.add(event.event_id)
        thread = apply_event(thread, event)
    if thread is None:
        raise KernelError("empty_transcript", "Transcript 为空")
    return thread
