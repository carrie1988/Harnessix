"""原 Git 审批完整历史的私有语义投影；不认证来源、不赋权、不产生任何写入。

父协调层负责原 Session/Router 全链、Execution 检查点及材料/Review 的认证读取。
AuthenticatedThreadHistory 是普通聚合，不是 MAC 证明；本模块仅解释已认证来源的
跨域语义，不能将调用方声明、摘要或 Fork 继承历史升级为原 Thread 的审批事实。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    Thread,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.reducer import apply_event, get_turn, pending_calls
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.domain.models import ApprovalOutcome, PolicyDecisionKind
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, canonical_digest
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.trusted_actions.agent_gateway_output import build_approval, build_result
from harnessix.trusted_actions.contracts import ActionAuditEvent, ActionExecutionOutcome
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2


@dataclass(frozen=True, slots=True)
class OriginalGitApprovalHistory:
    """仅保留原源事件和原决定；无序列化合同、签发标记或执行许可。"""

    state: Literal["pending", "approved", "denied", "cancelled"]
    request_event: AgentEvent = field(repr=False)
    decision_event: AgentEvent | None = field(repr=False)
    cancel_events: tuple[AgentEvent, ...] = field(repr=False)
    router_approval: ExecutionApprovalCheckpoint | None = field(repr=False)
    route_decision: ActionAuditEvent | None = field(repr=False)


def _require(condition: bool) -> None:
    """只暴露固定语义错误，不泄漏正文、路径、作者或底层校验消息。"""
    if not condition:
        raise KernelError("git_approval_history_changed", "Git审批历史无法核验")


def _request_matches(
    thread: Thread,
    event: AgentEvent,
    prepared: ProductGitPreparedLink,
    route: ActionRouteSnapshotV2,
) -> None:
    """用请求发生时的首个待处理 Call 和原构建器核对完整请求。"""
    core = prepared.plan.core
    _require(event.turn_id == core.turn_id and thread.active_turn_id == core.turn_id)
    turn = get_turn(thread, core.turn_id)
    calls = pending_calls(turn)
    _require(bool(calls) and calls[0] == core.call)
    assert isinstance(event.payload, ItemStarted)
    _require(event.payload.content == prepared.approval)
    rebuilt = build_approval(
        thread,
        turn,
        core.call,
        route,
        TrustedActionReview(diff_artifact=prepared.plan.review_artifact),
        presentation="patch_batch",
    )
    _require(rebuilt == prepared.approval)


def _is_request_event(event: AgentEvent, prepared: ProductGitPreparedLink) -> bool:
    """定位与原审批身份相关的开始事件；匹配本身不认定请求或授予权限。"""
    payload = event.payload
    return (
        isinstance(payload, ItemStarted)
        and isinstance(payload.content, TrustedActionApprovalRequestContent)
        and (
            payload.content.call_id == prepared.plan.core.call.call_id
            or payload.content.approval_id == prepared.approval.approval_id
            or payload.content.plan_id == prepared.approval.plan_id
        )
    )


def _replay_history(
    history: AuthenticatedThreadHistory,
    prepared: ProductGitPreparedLink,
    route: ActionRouteSnapshotV2,
    checkpoint: Callable[[], None],
) -> tuple[AgentEvent, tuple[AgentEvent, ...], Turn]:
    """从根逐事件重放全部后继；只从本 Thread 的原 ItemStarted 定位请求。"""
    core = prepared.plan.core
    thread: Thread | None = None
    request: AgentEvent | None = None
    following: list[AgentEvent] = []
    seen = set()
    waiting = False
    for event in history.events:
        # 回调不在语义错误包装内，取消、超时、Owner 错误须原实例传播。
        checkpoint()
        _require(type(event) is AgentEvent and event.event_id not in seen)
        seen.add(event.event_id)
        payload = event.payload
        is_request = _is_request_event(event, prepared)
        try:
            if is_request:
                _require(request is None and thread is not None)
                assert thread is not None
                _request_matches(thread, event, prepared, route)
                request = event
            elif request is not None and event.turn_id == core.turn_id:
                following.append(event)
            thread = apply_event(thread, event)
            if (
                request is not None
                and event.turn_id == core.turn_id
                and isinstance(payload, TurnStateChanged)
                and payload.status is TurnStatus.WAITING_APPROVAL
            ):
                calls = pending_calls(get_turn(thread, core.turn_id))
                _require(bool(calls) and calls[0] == core.call)
                waiting = True
        except (KernelError, ValueError, TypeError):
            raise KernelError("git_approval_history_changed", "Git审批历史无法核验") from None
    _require(thread is not None and thread == history.thread and request is not None and waiting)
    assert thread is not None and request is not None
    return request, tuple(following), get_turn(thread, core.turn_id)


def _require_route_event_binding(
    event: ActionAuditEvent,
    prepared: ProductGitPreparedLink,
    index: int,
    previous: ActionAuditEvent | None,
) -> None:
    """核对原计划、资源、策略及相邻链绑定，拒绝任何执行元数据。"""
    plan = prepared.plan.route
    execution = plan.execution
    _require(
        event.plan_id == execution.plan_id
        and event.plan_fingerprint == plan.fingerprint
        and event.resource_sha256 == plan.resources_sha256
        and event.policy_id == execution.policy.policy_id
        and event.policy_version == execution.policy.version
        and event.sequence == index
        and event.from_state == (previous.to_state if previous else None)
        and event.previous_digest == (previous.digest if previous else None)
    )
    _require(
        event.executor_id is None
        and event.output_sha256 is None
        and event.artifact_sha256 is None
        and event.external_action_id is None
        and event.reconciliation is None
    )


def _require_route_event_state(event: ActionAuditEvent, previous: ActionAuditEvent | None) -> None:
    """仅接受未决定初态与唯一审批迁移，不把其他 Route 终态解释为审批。"""
    if previous is None:
        _require(
            event.to_state == "pending_approval"
            and event.approval_outcome is None
            and event.approval_actor_sha256 is None
            and event.error_code is None
        )
    else:
        _require(
            event.from_state == "pending_approval"
            and event.to_state in {"ready", "denied"}
            and event.approval_outcome
            is (ApprovalOutcome.APPROVED if event.to_state == "ready" else ApprovalOutcome.REJECTED)
            and event.approval_actor_sha256 is not None
            and event.error_code == ("approval_rejected" if event.to_state == "denied" else None)
            and event.occurred_at >= previous.occurred_at
        )


def _route_history(
    prepared: ProductGitPreparedLink,
    route: ActionRouteSnapshotV2,
    events: tuple[ActionAuditEvent, ...],
    checkpoint: Callable[[], None],
) -> ActionAuditEvent | None:
    """核对已认证 Route 全链的窄域审批语义；不重复摘要或 MAC 认证算法。"""
    plan = prepared.plan.route
    execution = plan.execution
    _require(
        route.plan == plan and execution.policy.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    )
    _require(len(events) in {1, 2})
    previous: ActionAuditEvent | None = None
    for index, event in enumerate(events, 1):
        checkpoint()
        _require(type(event) is ActionAuditEvent)
        _require_route_event_binding(event, prepared, index, previous)
        _require_route_event_state(event, previous)
        previous = event
    assert previous is not None
    _require(
        (route.state, route.sequence, route.last_event_digest, route.updated_at)
        == (previous.to_state, previous.sequence, previous.digest, previous.occurred_at)
    )
    return events[1] if len(events) == 2 else None


def _router_decision(
    prepared: ProductGitPreparedLink,
    approval: ExecutionApprovalCheckpoint | None,
    route_decision: ActionAuditEvent | None,
) -> ExecutionApprovalCheckpoint:
    """Execution 请求指纹属于 Execution 域，不能与 Session 请求指纹直接等同。"""
    _require(type(approval) is ExecutionApprovalCheckpoint and route_decision is not None)
    assert approval is not None and route_decision is not None
    execution = prepared.plan.route.execution
    decision = approval.decision
    _require(
        approval.plan_id == execution.plan_id
        and approval.plan_fingerprint == execution.fingerprint
        and decision.request_fingerprint == execution.fingerprint
        and bool(decision.actor.strip())
        and decision.outcome is route_decision.approval_outcome
        and canonical_digest(decision.actor) == route_decision.approval_actor_sha256
        and decision.decided_at == route_decision.occurred_at
    )
    return approval


def _results(
    following: tuple[AgentEvent, ...],
    prepared: ProductGitPreparedLink,
) -> tuple[AgentEvent, ...]:
    """后继不得夹带其他调用结果、未知效果或新的执行效果。"""
    results = []
    for event in following:
        payload = event.payload
        if isinstance(payload, ItemStarted | ItemFinished) and isinstance(
            payload.content, ToolResultContent
        ):
            _require(payload.content.call_id == prepared.plan.core.call.call_id)
            results.append(event)
    return tuple(results)


def _human_decision(
    request: AgentEvent,
    finish: AgentEvent,
    approval: ExecutionApprovalCheckpoint,
    route: ActionRouteSnapshotV2,
    turn: Turn,
    following: tuple[AgentEvent, ...],
    prepared: ProductGitPreparedLink,
) -> Literal["approved", "denied"]:
    """原 Reducer 已核验请求不可变、决定时间及原 Turn 截止；再核对三方事实。"""
    payload = finish.payload
    assert isinstance(payload, ItemFinished)
    assert isinstance(request.payload, ItemStarted)
    _require(payload.status is ItemStatus.COMPLETED)
    content = payload.content
    _require(
        isinstance(content, TrustedActionApprovalRequestContent) and content.decision is not None
    )
    assert isinstance(content, TrustedActionApprovalRequestContent) and content.decision is not None
    decision = content.decision
    _require(
        decision.request_fingerprint == prepared.approval.request_fingerprint
        and (decision.outcome, decision.actor, decision.reason, decision.decided_at)
        == (
            approval.decision.outcome,
            approval.decision.actor,
            approval.decision.reason,
            approval.decision.decided_at,
        )
        and decision.actor != "system.cancel"
        and content.route_state == route.state
        and content.model_copy(update={"decision": None, "route_state": "pending_approval"})
        == request.payload.content
    )
    results = _results(following, prepared)
    if decision.outcome is ApprovalOutcome.APPROVED:
        _require(
            route.state == "ready"
            and turn.status in {TurnStatus.WAITING_APPROVAL, TurnStatus.EXECUTING_TOOLS}
            and not results
            and not any(
                isinstance(e.payload, TurnStateChanged)
                and e.payload.status is TurnStatus.CANCELLING
                for e in following
            )
        )
        return "approved"
    _require(decision.outcome is ApprovalOutcome.REJECTED and route.state == "denied")
    for event in results:
        payload = event.payload
        assert isinstance(payload, ItemStarted | ItemFinished)
        result = payload.content
        assert isinstance(result, ToolResultContent)
        # 原 denied 结算有 failed 类型化元数据；它不是已执行或未知效果。
        _require(result.trusted_action is not None)
        assert result.trusted_action is not None
        expected = build_result(
            route,
            prepared.plan.core.call,
            ActionExecutionOutcome(kind="failed", error_code="approval_rejected"),
            origin=result.trusted_action.origin,
            approval=content,
        )
        _require(
            result == expected
            and (not isinstance(payload, ItemFinished) or payload.status is ItemStatus.COMPLETED)
        )
    return "denied"


def _require_cancelled_result(event: AgentEvent) -> None:
    """核对已完成的原取消结果；正文不得携带任何效果、输出或工件声明。"""
    assert isinstance(event.payload, ItemFinished) and isinstance(
        event.payload.content, ToolResultContent
    )
    content = event.payload.content
    _require(
        event.payload.status is ItemStatus.COMPLETED
        and content.outcome == "cancelled"
        and content.output is None
        and content.action_id is None
        and content.patch is None
        and content.patch_batch is None
        and content.process is None
        and content.trusted_action is None
        and content.diff_artifact is None
        and content.error is not None
        and content.error.code == "cancelled"
    )


def _cancelled(
    finish: AgentEvent,
    following: tuple[AgentEvent, ...],
    turn: Turn,
    prepared: ProductGitPreparedLink,
    approval: ExecutionApprovalCheckpoint,
) -> tuple[AgentEvent, ...]:
    """仅审批前完整结算的四个原源事件；系统检查点不能伪装成人工决定。"""
    payload = finish.payload
    assert isinstance(payload, ItemFinished)
    _require(
        payload.status is ItemStatus.CANCELLED
        and payload.content == prepared.approval
        and turn.status is TurnStatus.CANCELLED
        and approval.decision.outcome is ApprovalOutcome.REJECTED
        and approval.decision.actor == "system.cancel"
        and approval.decision.reason == "turn_cancelled"
    )
    cancellations = []
    terminals = []
    previous_status = TurnStatus.EXECUTING_TOOLS
    for event in following:
        if isinstance(event.payload, TurnStateChanged):
            if event.payload.status is TurnStatus.CANCELLING:
                _require(previous_status is TurnStatus.WAITING_APPROVAL)
                cancellations.append(event)
            elif event.payload.status is TurnStatus.CANCELLED:
                terminals.append(event)
            previous_status = event.payload.status
    results = _results(following, prepared)
    completed = [event for event in results if isinstance(event.payload, ItemFinished)]
    _require(len(cancellations) == len(terminals) == len(completed) == 1 and len(results) == 2)
    result = completed[0]
    _require_cancelled_result(result)
    cancelling, terminal = cancellations[0], terminals[0]
    _require(
        cancelling.sequence
        < finish.sequence
        < results[0].sequence
        < result.sequence
        < terminal.sequence
    )
    # 检查点发生在原取消请求与 Session 结算之间，不要求两个库的事件时间相等。
    _require(cancelling.occurred_at <= approval.decision.decided_at <= finish.occurred_at)
    return cancelling, finish, result, terminal


def interpret_git_approval_history(
    history: AuthenticatedThreadHistory,
    prepared: ProductGitPreparedLink,
    route: ActionRouteSnapshotV2,
    route_events: tuple[ActionAuditEvent, ...],
    approval: ExecutionApprovalCheckpoint | None,
    *,
    checkpoint: Callable[[], None],
) -> OriginalGitApprovalHistory:
    """消费原完整已认证历史，只形成事实；不验证 MAC、修复或签发批准。"""
    checkpoint()
    _require(
        type(history) is AuthenticatedThreadHistory
        and type(prepared) is ProductGitPreparedLink
        and type(route) is ActionRouteSnapshotV2
        and type(route_events) is tuple
        and type(history.events) is tuple
        and history.thread.thread_id == prepared.plan.core.thread_id
        and prepared.approval.decision is None
        and prepared.approval.route_state == "pending_approval"
    )
    route_decision = _route_history(prepared, route, route_events, checkpoint)
    request, following, turn = _replay_history(history, prepared, route, checkpoint)
    assert isinstance(request.payload, ItemStarted)
    finishes = tuple(
        event
        for event in following
        if isinstance(event.payload, ItemFinished)
        and event.payload.item_id == request.payload.item_id
    )
    _require(len(finishes) <= 1)
    cancel_events: tuple[AgentEvent, ...] = ()
    decision_event = None
    if not finishes:
        _require(
            approval is None
            and route_decision is None
            and route.state == "pending_approval"
            and turn.status is TurnStatus.WAITING_APPROVAL
            and history.thread.active_turn_id == turn.turn_id
            and not _results(following, prepared)
        )
        state: Literal["pending", "approved", "denied", "cancelled"] = "pending"
    else:
        approval = _router_decision(prepared, approval, route_decision)
        finish = finishes[0]
        assert isinstance(finish.payload, ItemFinished)
        if finish.payload.status is ItemStatus.CANCELLED:
            _require(route.state == "denied")
            cancel_events = _cancelled(finish, following, turn, prepared, approval)
            state = "cancelled"
        else:
            state = _human_decision(request, finish, approval, route, turn, following, prepared)
            decision_event = finish
            if state == "approved":
                _require(
                    history.thread.active_turn_id == turn.turn_id and history.thread.archive is None
                )
    checkpoint()
    return OriginalGitApprovalHistory(
        state, request, decision_event, cancel_events, approval, route_decision
    )
