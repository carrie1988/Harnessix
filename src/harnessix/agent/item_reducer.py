"""校验Item开始与完成事件并生成不可变Turn投影；不持久化事件或执行Tool。"""

from __future__ import annotations

from datetime import timedelta

from harnessix.agent.approvals import approval_for, approval_matches, request_fingerprint
from harnessix.agent.batch_patching import validate_effect
from harnessix.agent.models import (
    TERMINAL_TURNS,
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
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    QuestionAnswerContent,
    QuestionRequestContent,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer_support import (
    _validate_process_result,
    _validate_process_state,
    pending_calls,
    require,
)
from harnessix.context.tool_result_contracts import ModelHistoryInspectionV2
from harnessix.context.tool_result_view import history_items
from harnessix.domain.models import (
    ApprovalOutcome,
    EffectClass,
)
from harnessix.patches.bridge_contracts import call_request_id
from harnessix.processes.bridge_contracts import PROCESS_AGENT_FRONTENDS


def _append_step_item_before_pending_steering(
    thread: Thread, turn: Turn, item: Item
) -> tuple[Item, ...]:
    """模型输出按语义位于当前步骤内收到的Steering之前。"""

    inspections = [
        inspection
        for inspection in turn.model_history_inspections
        if inspection.model_step == turn.model_steps
    ]
    if len(inspections) != 1:
        return (*turn.items, item)
    inspection = inspections[0]
    consumed = (
        inspection.raw_history_items
        if isinstance(inspection, ModelHistoryInspectionV2)
        else inspection.history_items
    )
    current_history = history_items(thread)
    if consumed > len(current_history):
        return (*turn.items, item)
    pending_steering = {
        candidate.item_id
        for candidate in current_history[consumed:]
        if isinstance(candidate.content, TextContent) and candidate.content.kind == "user_message"
    }
    for index, candidate in enumerate(turn.items):
        if candidate.item_id in pending_steering:
            return (*turn.items[:index], item, *turn.items[index:])
    return (*turn.items, item)


def _start_item(thread: Thread, turn: Turn, event: AgentEvent, payload: ItemStarted) -> Turn:
    """校验Item与当前Turn阶段、调用及审批事实的绑定后追加开始态投影。"""
    require(all(i.item_id != payload.item_id for i in turn.items), "Item ID 已存在")
    content = payload.content
    if isinstance(content, TextContent):
        if content.kind == "user_message":
            if turn.status == TurnStatus.ACCEPTED and not turn.items:
                require(not turn.items, "Turn初始输入必须是首个Item")
            else:
                require(event.schema_version >= 19, "运行中Steering需要Agent Event v19")
                require(
                    turn.status
                    in {
                        TurnStatus.ACCEPTED,
                        TurnStatus.PREPARING_CONTEXT,
                        TurnStatus.CALLING_MODEL,
                        TurnStatus.EXECUTING_TOOLS,
                        TurnStatus.WAITING_APPROVAL,
                        TurnStatus.WAITING_ACTION,
                        TurnStatus.WAITING_INPUT,
                    },
                    "当前Turn状态不接受Steering",
                )
        else:
            require(turn.status == TurnStatus.CALLING_MODEL, "模型 Item 只能在模型步骤内开始")
    elif isinstance(content, ToolCallContent):
        require(turn.status == TurnStatus.CALLING_MODEL, "Tool Call 只能由模型步骤产生")
        require(
            all(
                not isinstance(item.content, ToolCallContent)
                or item.content.call_id != content.call_id
                for item in history_items(thread)
            ),
            "Tool Call ID 在Thread模型历史内重复",
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
                else (
                    call.tool in PROCESS_AGENT_FRONTENDS
                    if isinstance(content, ProcessApprovalRequestContent)
                    else call.tool
                    == (
                        "apply_patch_batch"
                        if isinstance(content, PatchBatchApprovalRequestContent)
                        else "apply_patch"
                    )
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
    elif isinstance(content, ProcessActionStateContent):
        calls = pending_calls(turn)
        require(bool(calls) and calls[0].call_id == content.call_id, "Process状态与当前调用不匹配")
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "存在未结算 Item")
        _validate_process_state(thread, turn, calls[0], content)
    elif isinstance(content, QuestionRequestContent):
        calls = pending_calls(turn)
        require(event.schema_version >= 19, "持久提问需要Agent Event v19")
        require(turn.status == TurnStatus.EXECUTING_TOOLS, "提问只能在工具执行边界生成")
        require(bool(calls) and calls[0].call_id == content.call_id, "提问与当前调用不匹配")
        require(calls[0].tool == "ask_user", "提问只能来自ask_user工具")
        require(
            not any(
                isinstance(item.content, QuestionRequestContent)
                and item.content.call_id == content.call_id
                for item in turn.items
            ),
            "当前调用已经生成提问",
        )
    elif isinstance(content, QuestionAnswerContent):
        calls = pending_calls(turn)
        questions = [
            item.content
            for item in turn.items
            if isinstance(item.content, QuestionRequestContent)
            and item.status == ItemStatus.COMPLETED
            and item.content.question_id == content.question_id
        ]
        require(event.schema_version >= 19, "持久回答需要Agent Event v19")
        require(turn.status == TurnStatus.WAITING_INPUT, "回答只能在等待输入状态提交")
        require(len(questions) == 1, "回答缺少唯一提问")
        require(
            bool(calls)
            and calls[0].call_id == content.call_id
            and questions[0].call_id == content.call_id,
            "回答与当前调用不匹配",
        )
        require(
            not any(
                isinstance(item.content, QuestionAnswerContent)
                and item.content.question_id == content.question_id
                for item in turn.items
            ),
            "提问已经答复",
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
        _validate_process_result(turn, calls[0], content)
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
                if isinstance(approval.content, ProcessApprovalRequestContent):
                    require(content.process is not None, "Process成功必须有Action终止证据")
                require(
                    approval.content.decision is not None
                    and approval.content.decision.outcome == ApprovalOutcome.APPROVED,
                    "未经批准的调用不能成功",
                )
    item = Item(item_id=payload.item_id, status=ItemStatus.STARTED, content=content)
    step_item = (isinstance(content, TextContent) and content.kind != "user_message") or isinstance(
        content, ToolCallContent | ToolResultContent
    )
    items = (
        _append_step_item_before_pending_steering(thread, turn, item)
        if step_item
        else (*turn.items, item)
    )
    return turn.model_copy(update={"items": items})


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
        cleared: dict[str, object] = {"decision": None}
        if isinstance(original.content, ProcessApprovalRequestContent):
            cleared["action_status"] = original.content.action_status
        require(
            original.content == payload.content.model_copy(update=cleared),
            "审批请求身份与指纹不可变",
        )
        decision = payload.content.decision
        if payload.status == ItemStatus.COMPLETED:
            require(turn.status == TurnStatus.WAITING_APPROVAL, "审批答复只能在等待状态提交")
            require(decision is not None, "审批答复缺少决定")
            assert decision is not None
            expected_fingerprint = (
                original.content.plan.action_fingerprint
                if isinstance(original.content, ProcessApprovalRequestContent)
                else original.content.request_fingerprint
            )
            require(decision.request_fingerprint == expected_fingerprint, "审批决定指纹不匹配")
            require(decision.decided_at == event.occurred_at, "审批决定时间必须与事件一致")
            require(
                turn.created_at <= event.occurred_at,
                "审批决定早于 Turn 创建时间",
            )
            if not isinstance(original.content, ProcessApprovalRequestContent):
                require(
                    event.occurred_at
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
