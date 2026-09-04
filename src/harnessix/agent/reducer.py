from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from uuid import UUID

from harnessix.agent.approvals import approval_for, approval_matches, request_fingerprint
from harnessix.agent.batch_patching import validate_effect
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    TURN_TRANSITIONS,
    AgentEvent,
    ApprovalContent,
    ApprovalRequestContent,
    CompactionContent,
    ErrorContent,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    PlanContent,
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
from harnessix.agent.usage import (
    ModelAttempt,
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
)
from harnessix.domain.models import ApprovalOutcome, EffectClass
from harnessix.patches.bridge_contracts import call_request_id


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
    elif isinstance(content, ApprovalContent):
        calls = pending_calls(turn)
        require(turn.status == TurnStatus.EXECUTING_TOOLS, "审批请求只能在执行边界生成")
        require(bool(calls) and calls[0].call_id == content.call_id, "审批与当前调用不匹配")
        call = calls[0]
        require(
            call.requires_approval
            and (
                call.effect_class == EffectClass.READ_ONLY
                if isinstance(content, ApprovalRequestContent)
                else call.tool
                == (
                    "apply_patch_batch"
                    if isinstance(content, PatchBatchApprovalRequestContent)
                    else "apply_patch"
                )
                and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
            ),
            "审批类型与工具效果不匹配",
        )
        require(call.tool_fingerprint is not None, "审批缺少工具契约指纹")
        require(approval_for(turn, call) is None, "调用已存在审批请求")
        require(
            all(
                not isinstance(i.content, ApprovalContent)
                or i.content.approval_id != content.approval_id
                for i in turn.items
            ),
            "审批 ID 重复",
        )
        require(content.decision is None, "审批请求不能预置决定")
        require(
            approval_matches(thread, turn, call, content),
            "审批指纹不匹配",
        )
    elif isinstance(content, PlanContent | CompactionContent):
        require(
            turn.status == TurnStatus.PREPARING_CONTEXT, "Plan/Compaction 只能在准备上下文时记录"
        )
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "存在未结算 Item")
        if isinstance(content, PlanContent):
            plans = [
                i
                for t in thread.turns
                for i in t.items
                if isinstance(i.content, PlanContent) and i.status == ItemStatus.COMPLETED
            ]
            require(
                content.supersedes == (plans[-1].item_id if plans else None),
                "Plan 必须引用最新完成的计划",
            )
        else:
            sources = {
                i.item_id: i
                for t in thread.turns
                if t.status in TERMINAL_TURNS
                for i in t.items
                if i.status == ItemStatus.COMPLETED
                and isinstance(i.content, TextContent | ToolCallContent | ToolResultContent)
            }
            require(
                all(item_id in sources for item_id in content.source_item_ids),
                "Compaction 来源必须属于已终结 Turn 的完成消息或工具 Item",
            )
            selected = [sources[item_id].content for item_id in content.source_item_ids]
            selected_calls = {c.call_id for c in selected if isinstance(c, ToolCallContent)}
            results = {c.call_id for c in selected if isinstance(c, ToolResultContent)}
            require(selected_calls == results, "Compaction 不能拆散工具调用与结果")
    elif isinstance(content, ErrorContent):
        pass  # 失败可发生于任意活跃阶段；终态校验要求错误事实与终态一致。
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
        if content.patch_batch is not None:
            validate_effect(thread, turn, calls[0], content)
        if content.patch is not None:
            effect, call = content.patch, calls[0]
            require(
                call.tool == "apply_patch"
                and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
                and call.requires_approval
                and call.tool_fingerprint is not None,
                "效果证据仅适用于强制审批的 Patch 调用",
            )
            require(
                effect.request_id
                == call_request_id(
                    thread.thread_id,
                    turn.turn_id,
                    call.call_id,
                    request_fingerprint(thread, turn, call),
                ),
                "效果证据调用归属错误",
            )
            approval = approval_for(turn, call)
            if approval is not None:
                require(
                    isinstance(approval.content, PatchApprovalRequestContent),
                    "Patch 证据不能复用只读审批",
                )
                assert isinstance(approval.content, PatchApprovalRequestContent)
                plan = approval.content.plan
                require(
                    (
                        effect.workspace_id,
                        effect.plan_id,
                        effect.request_id,
                        effect.approval_fingerprint,
                    )
                    == (
                        plan.workspace_id,
                        plan.plan_id,
                        plan.request_id,
                        plan.approval_fingerprint,
                    ),
                    "效果证据与审批计划不匹配",
                )
            require(
                effect.origin == "recovery" or turn.status == TurnStatus.EXECUTING_TOOLS,
                "执行证据只能在执行状态发布",
            )
            if content.outcome == "succeeded":
                require(effect.state in {"applied", "observed_after"}, "成功结果缺少已应用证据")
                require(
                    effect.origin == "recovery" or effect.state == "applied", "执行不能伪造恢复观察"
                )
            elif content.outcome == "failed":
                require(
                    effect.state
                    in {"pending", "approved", "rejected", "failed", "observed_before"},
                    "已知失败与效果状态不一致",
                )
            else:
                require(content.outcome == "unknown", "Patch 取消不等于文件效果取消")
        if content.outcome == "succeeded":
            require(
                turn.status == TurnStatus.EXECUTING_TOOLS
                or (content.patch is not None and content.patch.origin == "recovery")
                or (content.patch_batch is not None and content.patch_batch.origin == "recovery"),
                "执行阶段之外不能记录普通成功结果",
            )
            if calls[0].requires_approval:
                approval = approval_for(turn, calls[0])
                require(
                    approval is not None and approval.status == ItemStatus.COMPLETED,
                    "成功结果之前必须持久记录审批决定",
                )
                assert approval is not None and isinstance(approval.content, ApprovalContent)
                if isinstance(approval.content, PatchBatchApprovalRequestContent):
                    require(content.patch_batch is not None, "整组成功必须有类型化效果证据")
                if isinstance(approval.content, PatchApprovalRequestContent):
                    require(content.patch is not None, "Patch 成功必须有类型化效果证据")
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
    if isinstance(original.content, ApprovalContent):
        require(
            isinstance(payload.content, ApprovalContent),
            "审批 Item 类型不可改变",
        )
        assert isinstance(payload.content, ApprovalContent)
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
        require(all(a.status != "running" for a in turn.model_attempts), "存在未结算模型尝试")
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "存在未结算 Item")
        require(not pending_calls(turn), "存在未配对 Tool Call")
        if target == TurnStatus.COMPLETED:
            require(payload.error is None, "成功终态不能携带错误")
            require(
                not any(
                    isinstance(i.content, ToolResultContent)
                    and i.content.patch_batch is not None
                    and i.content.patch_batch.execution is not None
                    and i.content.patch_batch.execution.run.stop_reason != "completed"
                    for i in turn.items
                ),
                "组运行非正常终止不能冒充成功 Turn",
            )
            require(
                not any(
                    isinstance(i.content, ToolResultContent)
                    and (
                        (i.content.patch is not None and i.content.patch.origin == "recovery")
                        or (
                            i.content.patch_batch is not None
                            and i.content.patch_batch.origin == "recovery"
                        )
                    )
                    for i in turn.items
                ),
                "恢复效果不能把中断执行冒充成功 Turn",
            )
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
        if event.schema_version >= 3:
            errors = [
                i.content
                for i in turn.items
                if isinstance(i.content, ErrorContent) and i.status == ItemStatus.COMPLETED
            ]
            if target == TurnStatus.COMPLETED:
                require(not errors, "存在终止错误事实的 Turn 不能成功完成")
            else:
                require(
                    bool(errors) and errors[-1].failure == payload.error,
                    "非成功终态必须先记录一致的 Error Item",
                )
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


def _model_attempt(turn: Turn, event: AgentEvent) -> Turn:
    payload = event.payload
    if isinstance(payload, ModelAttemptStarted):
        require(turn.status == TurnStatus.CALLING_MODEL, "模型尝试只能在调用状态开始")
        require(
            payload.step == turn.model_steps and payload.step > turn.usage_step,
            "尝试不属于当前开放步骤",
        )
        require(all(a.status != "running" for a in turn.model_attempts), "上一尝试尚未结束")
        previous = [a for a in turn.model_attempts if a.step == payload.step]
        require(payload.index == len(previous) + 1, "尝试序号不连续")
        require(not previous or previous[-1].status == "failed", "只能在失败尝试后开始重试")
        require(turn.usage.total_tokens < turn.budget.max_tokens, "模型尝试的已知 Token 预算耗尽")
        created = ModelAttempt(**payload.model_dump(exclude={"type"}), started_at=event.occurred_at)
        return turn.model_copy(update={"model_attempts": (*turn.model_attempts, created)})
    assert isinstance(payload, ModelUsageObserved | ModelAttemptFinished)
    require(
        turn.status in {TurnStatus.CALLING_MODEL, TurnStatus.CANCELLING}, "不在模型尝试结算状态"
    )
    attempt = next((a for a in turn.model_attempts if a.attempt_id == payload.attempt_id), None)
    require(attempt is not None, "模型尝试尚未开始")
    assert attempt is not None
    require(attempt.status == "running", "模型尝试已结算")
    require(attempt.step == turn.model_steps, "尝试不属于当前模型步骤")
    usage = turn.usage
    if isinstance(payload, ModelUsageObserved):
        try:
            payload.usage.validate_successor(attempt.usage)
            billing = payload.billing if payload.billing is not None else attempt.billing
            billing.validate_successor(attempt.billing)
            billing.validate_usage(payload.usage)
        except ValueError:
            raise KernelError("invalid_event", "模型用量或计费元数据冲突") from None
        for field in ("actual_model", "response_id"):
            before, after = getattr(attempt, field), getattr(payload, field)
            require(before is None or after is None or before == after, "尝试响应身份发生变化")
        usage = Usage(
            input_tokens=turn.usage.input_tokens
            + (payload.usage.input_tokens or 0)
            - (attempt.usage.input_tokens or 0),
            output_tokens=turn.usage.output_tokens
            + (payload.usage.output_tokens or 0)
            - (attempt.usage.output_tokens or 0),
        )
        attempt = attempt.model_copy(
            update={
                "usage": payload.usage,
                "billing": billing,
                "actual_model": payload.actual_model or attempt.actual_model,
                "response_id": payload.response_id or attempt.response_id,
            }
        )
    else:
        if payload.outcome == "completed":
            require(attempt.usage.completeness == "complete", "成功尝试需要完整用量")
            require(
                attempt.actual_model is not None and attempt.response_id is not None,
                "成功尝试缺少响应身份",
            )
        attempt = attempt.model_copy(
            update={
                "status": payload.outcome,
                "error": payload.error,
                "finished_at": event.occurred_at,
            }
        )
    return turn.model_copy(
        update={
            "usage": usage,
            "model_attempts": tuple(
                attempt if a.attempt_id == attempt.attempt_id else a for a in turn.model_attempts
            ),
        }
    )


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
        elif isinstance(payload, ModelAttemptStarted | ModelUsageObserved | ModelAttemptFinished):
            if isinstance(payload, ModelAttemptStarted):
                require(
                    all(
                        a.attempt_id != payload.attempt_id
                        for t in thread.turns
                        for a in t.model_attempts
                    ),
                    "尝试 ID 在 Thread 内重复",
                )
            turn = _model_attempt(turn, event)
        elif isinstance(payload, UsageRecorded):
            require(turn.status == TurnStatus.CALLING_MODEL, "用量只能在模型步骤内记录")
            require(payload.step == turn.model_steps, "用量不属于当前模型步骤")
            require(payload.step > turn.usage_step, "用量重复记账")
            attempts = [a for a in turn.model_attempts if a.step == payload.step]
            if attempts:
                last = attempts[-1]
                require(last.status == "completed", "模型响应完成前必须结算成功尝试")
                require(
                    payload.usage.input_tokens == last.usage.input_tokens
                    and payload.usage.output_tokens == last.usage.output_tokens,
                    "响应用量与尝试事实不一致",
                )
            turn = turn.model_copy(
                update={
                    "usage_step": payload.step,
                    "usage": turn.usage
                    if attempts
                    else Usage(
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
