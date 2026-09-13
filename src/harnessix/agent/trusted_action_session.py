"""Trusted Action Gateway与Agent Session双账本的一致性操作。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.approvals import approval_for
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    EventDraft,
    ItemFinished,
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TrustedActionEffect,
    Turn,
    TurnStatus,
)
from harnessix.agent.ports import TrustedActionGateway
from harnessix.agent.reducer_support import get_turn, pending_calls
from harnessix.domain.models import ApprovalDecision, ToolDescriptor
from harnessix.session.ports import SessionStore

type LockFactory = Callable[[UUID], asyncio.Lock]
type ToolContractValidator = Callable[[ToolCallContent], None]
type FaultInjector = Callable[[str], None]
type PendingCallExecutor = Callable[[UUID, UUID, CancelToken], Awaitable[Turn | None]]

_PREPARE_FAILURES = frozenset(
    {"tool_invalid_arguments", "action_resource_invalid", "raw_secret_rejected"}
)


@dataclass(frozen=True, slots=True)
class TrustedActionSessionState:
    """跨账本操作依赖及构造时冻结的Tool目录。"""

    gateway: TrustedActionGateway
    store: SessionStore
    lock: LockFactory
    validate_tool_contract: ToolContractValidator
    fault: FaultInjector
    definitions: tuple[ToolDescriptor, ...]
    tool_names: frozenset[str]


def build_session_state(
    gateway: TrustedActionGateway,
    store: SessionStore,
    lock: LockFactory,
    validate_tool_contract: ToolContractValidator,
    fault: FaultInjector,
) -> TrustedActionSessionState:
    definitions = gateway.definitions()
    return TrustedActionSessionState(
        gateway=gateway,
        store=store,
        lock=lock,
        validate_tool_contract=validate_tool_contract,
        fault=fault,
        definitions=definitions,
        tool_names=frozenset(item.name for item in definitions),
    )


async def prepare_action(
    state: TrustedActionSessionState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    token: CancelToken,
) -> TrustedActionApprovalRequestContent | ToolResultContent:
    """只把可公开参数失败转换为Tool Result；Store或Router故障失败关闭。"""

    state.fault("runtime.before_trusted_action_prepare")
    try:
        prepared = await state.gateway.prepare(thread, turn, call, token)
    except KernelError as error:
        if error.code not in _PREPARE_FAILURES:
            raise
        prepared = ToolResultContent(
            call_id=call.call_id,
            outcome="failed",
            error=error.to_failure(),
        )
    state.fault("runtime.after_trusted_action_prepare")
    return prepared


async def sync_action_decision(
    state: TrustedActionSessionState, thread_id: UUID, turn_id: UUID
) -> Turn | None:
    """恢复Router已提交、Session尚未提交的审批决定。"""

    async with state.lock(thread_id):
        thread = await state.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        calls = pending_calls(turn)
        if turn.status != TurnStatus.WAITING_APPROVAL or not calls:
            return None
        item = approval_for(turn, calls[0])
        if item is None or not isinstance(item.content, TrustedActionApprovalRequestContent):
            return None
        if item.status != ItemStatus.STARTED or item.content.decision is not None:
            return None
        call = calls[0]
        state.validate_tool_contract(call)
        projected = state.gateway.sync_decision(thread, turn, call, item.content)
        if projected is None:
            return turn
        assert projected.decision is not None
        updated = await state.store.append(
            thread_id,
            [_decision_event(turn_id, item.item_id, projected)],
            expected_sequence=thread.sequence,
        )
        return get_turn(updated, turn_id)


async def resume_action_execution(
    state: TrustedActionSessionState,
    thread: Thread,
    turn: Turn,
    calls: list[ToolCallContent],
    execute_calls: PendingCallExecutor,
) -> Turn | None:
    """重开时只续跑属于Gateway的执行边界，并返回最新Turn。"""

    if (
        turn.status is not TurnStatus.EXECUTING_TOOLS
        or not calls
        or calls[0].tool not in state.tool_names
    ):
        return None
    waiting = await execute_calls(thread.thread_id, turn.turn_id, CancelToken())
    if waiting is not None:
        return waiting
    return get_turn(await state.store.get_thread(thread.thread_id), turn.turn_id)


async def record_action_decision(
    state: TrustedActionSessionState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    item_id: UUID,
    content: TrustedActionApprovalRequestContent,
    decision: ApprovalDecision,
) -> Turn:
    """Router先提交批准权威；Session随后以原时间戳CAS追加相同投影。"""

    state.fault("runtime.before_approval_decision")
    projected = state.gateway.decide(thread, turn, call, content, decision)
    assert projected.decision is not None
    state.fault("runtime.after_trusted_action_decision")
    updated = await state.store.append(
        thread.thread_id,
        [_decision_event(turn.turn_id, item_id, projected)],
        expected_sequence=thread.sequence,
    )
    state.fault("runtime.after_approval_decision")
    return get_turn(updated, turn.turn_id)


def _decision_event(
    turn_id: UUID,
    item_id: UUID,
    projected: TrustedActionApprovalRequestContent,
) -> EventDraft:
    assert projected.decision is not None
    return EventDraft(
        turn_id=turn_id,
        occurred_at=projected.decision.decided_at,
        payload=ItemFinished(
            item_id=item_id,
            status=ItemStatus.COMPLETED,
            content=projected,
        ),
    )


async def execute_action(
    state: TrustedActionSessionState,
    thread_id: UUID,
    turn_id: UUID,
    call: ToolCallContent,
    token: CancelToken,
) -> ToolResultContent:
    """从Session重取决定，拒绝调用方传入游离审批对象。"""

    thread = await state.store.get_thread(thread_id)
    turn = get_turn(thread, turn_id)
    item = approval_for(turn, call)
    if item is None or not isinstance(item.content, TrustedActionApprovalRequestContent):
        raise KernelError("approval_mismatch", "Trusted Action执行缺少审批计划")
    if item.status != ItemStatus.COMPLETED or item.content.decision is None:
        raise KernelError("approval_missing", "Trusted Action执行缺少已持久决定")
    state.fault("runtime.before_tool")
    result = await state.gateway.execute(thread, turn, call, item.content, token)
    state.fault("runtime.after_tool")
    return result


async def recover_action(
    state: TrustedActionSessionState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
) -> ToolResultContent | None:
    """终结时只观察或Reconcile；ready计划不会被此路径启动。"""

    item = approval_for(turn, call)
    content = item.content if item is not None else None
    approval = content if isinstance(content, TrustedActionApprovalRequestContent) else None
    try:
        state.validate_tool_contract(call)
        return await state.gateway.recover(thread, turn, call, approval, CancelToken())
    except Exception as error:
        return _unknown_recovery_result(call, approval, error)


def _unknown_recovery_result(
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent | None,
    error: Exception,
) -> ToolResultContent:
    failure = (
        error.to_failure()
        if isinstance(error, KernelError)
        else AgentFailure(
            code="trusted_action_recovery_failed",
            message="Trusted Action核对失败；原始错误未持久化",
        )
    )
    if approval is None:
        return ToolResultContent(call_id=call.call_id, outcome="unknown", error=failure)
    return ToolResultContent(
        call_id=call.call_id,
        outcome="unknown",
        error=failure,
        action_id=approval.plan_id,
        trusted_action=TrustedActionEffect(
            plan_id=approval.plan_id,
            plan_fingerprint=approval.plan_fingerprint,
            state="unknown",
            origin="recovery",
        ),
        diff_artifact=approval.diff_artifact,
    )
