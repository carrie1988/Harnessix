"""校验Turn状态、模型尝试和Context事件并生成不可变投影；不执行运行时编排。"""

from __future__ import annotations

from datetime import timedelta

from harnessix.agent.approvals import approval_for
from harnessix.agent.attempt_accounting import settle_observation
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    PROCESS_RESOLVED_STATUSES,
    TERMINAL_TURNS,
    TURN_TRANSITIONS,
    AgentEvent,
    ErrorContent,
    ItemStatus,
    ModelHistoryPrepared,
    ProcessApprovalRequestContent,
    QuestionAnswerContent,
    QuestionRequestContent,
    TextContent,
    Thread,
    ToolResultContent,
    Turn,
    TurnStateChanged,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer_support import (
    _process_approval,
    _process_effects,
    pending_calls,
    require,
)
from harnessix.agent.usage import (
    ModelAttempt,
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
)
from harnessix.context.compaction_ledger_contracts import COMPACTION_OPEN
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.contracts import ContextPrepared
from harnessix.context.tool_result_contracts import ModelHistoryInspectionV2
from harnessix.context.tool_result_view import history_items


def _change_state(thread: Thread, turn: Turn, event: AgentEvent, payload: TurnStateChanged) -> Turn:
    """执行Turn状态迁移守卫；非法倒退、未结算副作用和超预算迁移均失败关闭。"""
    target = payload.status
    allowed = set(TURN_TRANSITIONS.get(turn.status, set()))
    if turn.status != TurnStatus.CANCELLING:
        allowed.update({TurnStatus.CANCELLING, TurnStatus.FAILED, TurnStatus.INTERRUPTED})
    require(target in allowed, f"非法 Turn 状态转换：{turn.status} → {target}")
    model_reentry = (
        turn.status == TurnStatus.CALLING_MODEL and target == TurnStatus.PREPARING_CONTEXT
    )
    if not model_reentry:
        require(payload.reason == "normal", "普通状态转换不能携带模型重入原因")
    if model_reentry and payload.reason == "context_overflow":
        attempts = [attempt for attempt in turn.model_attempts if attempt.step == turn.model_steps]
        inspections = [
            inspection
            for inspection in turn.model_history_inspections
            if inspection.model_step == turn.model_steps
        ]
        raw_items_before = (
            inspections[0].raw_history_items
            if len(inspections) == 1 and isinstance(inspections[0], ModelHistoryInspectionV2)
            else inspections[0].history_items
            if len(inspections) == 1
            else -1
        )
        require(event.schema_version >= 15, "Context Overflow恢复需要Agent Event v15")
        require(payload.error is None, "Context Overflow恢复状态不能携带终止错误")
        require(
            bool(attempts)
            and attempts[-1].status == "failed"
            and attempts[-1].error is not None
            and attempts[-1].error.code == "provider_context_overflow",
            "只有已记账的Context Overflow才能进入压缩恢复",
        )
        require(all(attempt.status != "running" for attempt in attempts), "模型尝试尚未结算")
        require(turn.usage_step < turn.model_steps, "已完成响应不能进入Context Overflow恢复")
        require(len(history_items(thread)) == raw_items_before, "模型已输出语义Item，禁止压缩重试")
        require(not pending_calls(turn), "Context Overflow恢复时存在未结算调用")
        require(all(c.status not in COMPACTION_OPEN for c in turn.compactions), "存在开放压缩")
    elif model_reentry:
        require(event.schema_version >= 19 and payload.reason == "steering", "模型重入原因无效")
        inspections = [
            inspection
            for inspection in turn.model_history_inspections
            if inspection.model_step == turn.model_steps
        ]
        history_count = (
            inspections[0].raw_history_items
            if len(inspections) == 1 and isinstance(inspections[0], ModelHistoryInspectionV2)
            else inspections[0].history_items
            if len(inspections) == 1
            else -1
        )
        new_history = history_items(thread)[history_count:]
        require(
            any(
                isinstance(item.content, TextContent) and item.content.kind == "user_message"
                for item in new_history
            ),
            "Steering重入缺少新用户输入",
        )
        require(not pending_calls(turn), "存在工具调用时不提前Steering重入")
    if target in TERMINAL_TURNS:
        require(all(a.status != "running" for a in turn.accounted_attempts), "存在未结算模型尝试")
        require(all(c.status not in COMPACTION_OPEN for c in turn.compactions), "存在开放压缩")
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
    if target == TurnStatus.WAITING_ACTION:
        calls = pending_calls(turn)
        require(bool(calls), "等待Action必须有未结算调用")
        approval = _process_approval(turn, calls[0])
        require(
            approval is not None
            and approval.status == ItemStatus.COMPLETED
            and isinstance(approval.content, ProcessApprovalRequestContent)
            and approval.content.decision is not None,
            "等待Action必须先投影唯一Action审批决定",
        )
        require(
            all(i.status != ItemStatus.STARTED for i in turn.items), "等待Action时存在未完成 Item"
        )
    if target == TurnStatus.WAITING_INPUT:
        calls = pending_calls(turn)
        require(event.schema_version >= 19, "等待输入需要Agent Event v19")
        require(bool(calls) and calls[0].tool == "ask_user", "等待输入缺少ask_user调用")
        questions = [
            item
            for item in turn.items
            if isinstance(item.content, QuestionRequestContent)
            and item.content.call_id == calls[0].call_id
        ]
        require(
            len(questions) == 1 and questions[0].status == ItemStatus.COMPLETED,
            "等待输入缺少完成的提问",
        )
        require(all(i.status != ItemStatus.STARTED for i in turn.items), "等待输入时存在未完成Item")
    if turn.status == TurnStatus.WAITING_INPUT and target == TurnStatus.EXECUTING_TOOLS:
        calls = pending_calls(turn)
        require(bool(calls), "回答之后缺少当前调用")
        question_contents = [
            item.content
            for item in turn.items
            if isinstance(item.content, QuestionRequestContent)
            and item.content.call_id == calls[0].call_id
        ]
        answers = [
            item.content
            for item in turn.items
            if isinstance(item.content, QuestionAnswerContent)
            and item.content.call_id == calls[0].call_id
        ]
        require(len(question_contents) == len(answers) == 1, "等待输入没有唯一回答")
    if turn.status == TurnStatus.WAITING_APPROVAL and target == TurnStatus.EXECUTING_TOOLS:
        calls = pending_calls(turn)
        require(bool(calls), "审批之后缺少当前调用")
        approval = approval_for(turn, calls[0])
        require(
            approval is not None and approval.status == ItemStatus.COMPLETED,
            "审批决定持久化前不能离开等待状态",
        )
        assert approval is not None
        require(
            not isinstance(approval.content, ProcessApprovalRequestContent),
            "Process审批后必须先进入持久Action等待",
        )
        require(
            event.occurred_at < turn.created_at + timedelta(seconds=turn.budget.timeout_seconds),
            "Turn 时间预算已耗尽",
        )
    if turn.status == TurnStatus.WAITING_ACTION and target == TurnStatus.EXECUTING_TOOLS:
        calls = pending_calls(turn)
        require(bool(calls), "Action终止后缺少当前调用")
        effects = _process_effects(turn, calls[0])
        require(
            bool(effects) and effects[-1].status in PROCESS_RESOLVED_STATUSES,
            "Action终止事实持久化前不能离开等待状态",
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
    attempt, input_delta, output_delta = settle_observation(attempt, payload, event.occurred_at)
    usage = Usage(
        input_tokens=turn.usage.input_tokens + input_delta,
        output_tokens=turn.usage.output_tokens + output_delta,
    )
    return turn.model_copy(
        update={
            "usage": usage,
            "model_attempts": tuple(
                attempt if a.attempt_id == attempt.attempt_id else a for a in turn.model_attempts
            ),
        }
    )


def _prepare_context(turn: Turn, payload: ContextPrepared) -> Turn:
    inspection = payload.inspection
    require(turn.status == TurnStatus.PREPARING_CONTEXT, "Context 只能在准备阶段记录")
    require(inspection.model_step == turn.model_steps + 1, "Context 不属于下一个模型步骤")
    require(
        all(existing.model_step != inspection.model_step for existing in turn.context_inspections),
        "同一模型步骤只能记录一份 Context",
    )
    return turn.model_copy(update={"context_inspections": (*turn.context_inspections, inspection)})


def _prepare_model_history(thread: Thread, turn: Turn, payload: ModelHistoryPrepared) -> Turn:
    inspection = payload.inspection
    require(turn.status == TurnStatus.PREPARING_CONTEXT, "模型历史只能在准备阶段记录")
    require(inspection.model_step == turn.model_steps + 1, "模型历史不属于下一个模型步骤")
    require(
        all(
            existing.model_step != inspection.model_step
            for existing in turn.model_history_inspections
        ),
        "同一模型步骤只能记录一份模型历史检查",
    )
    try:
        prepared = prepare_active_model_history(
            thread, inspection.model_step, inspection.policy, decisions=payload.decisions
        )
    except (KernelError, ValueError):
        raise KernelError("invalid_event", "模型历史决定无法由Session事实验证") from None
    require(prepared.inspection == inspection, "模型历史检查与Session事实不一致")
    require(prepared.new_decisions == payload.decisions, "Tool Result新决定与Session事实不一致")
    return turn.model_copy(
        update={
            "tool_result_view_decisions": (
                *turn.tool_result_view_decisions,
                *payload.decisions,
            ),
            "model_history_inspections": (*turn.model_history_inspections, inspection),
        }
    )
