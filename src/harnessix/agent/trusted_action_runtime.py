"""把Trusted Action跨账本编排接入Agent Runtime。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ApprovalContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.agent.ports import TrustedActionGateway
from harnessix.agent.trusted_action_session import (
    FaultInjector,
    LockFactory,
    PendingCallExecutor,
    ToolContractValidator,
    build_session_state,
    execute_action,
    prepare_action,
    record_action_decision,
    recover_action,
    resume_action_execution,
    sync_action_decision,
)
from harnessix.domain.models import ApprovalDecision, ToolDescriptor
from harnessix.session.ports import SessionStore

type TurnContinuation = Callable[[UUID, UUID, CancelToken], Awaitable[Turn | None]]


class TrustedActionSessionRuntime:
    """保持Router先行决策、Session CAS提交及恢复只核对的不变量。"""

    def __init__(
        self,
        gateway: TrustedActionGateway,
        store: SessionStore,
        lock: LockFactory,
        validate_tool_contract: ToolContractValidator,
        fault: FaultInjector,
    ) -> None:
        self._state = build_session_state(gateway, store, lock, validate_tool_contract, fault)

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(item.model_copy(deep=True) for item in self._state.definitions)

    def owns(self, call: ToolCallContent) -> bool:
        return call.tool in self._state.tool_names

    def handles(self, content: object) -> bool:
        return isinstance(content, TrustedActionApprovalRequestContent)

    async def prepare(
        self, thread: Thread, turn: Turn, call: ToolCallContent, token: CancelToken
    ) -> TrustedActionApprovalRequestContent | ToolResultContent:
        return await prepare_action(self._state, thread, turn, call, token)

    async def sync_decision(self, thread_id: UUID, turn_id: UUID) -> Turn | None:
        return await sync_action_decision(self._state, thread_id, turn_id)

    async def record_decision(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        item_id: UUID,
        content: TrustedActionApprovalRequestContent,
        decision: ApprovalDecision,
    ) -> Turn:
        return await record_action_decision(
            self._state, thread, turn, call, item_id, content, decision
        )

    async def execute(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        token: CancelToken,
    ) -> ToolResultContent:
        return await execute_action(self._state, thread_id, turn_id, call, token)

    async def recover(
        self, thread: Thread, turn: Turn, call: ToolCallContent
    ) -> ToolResultContent | None:
        return await recover_action(self._state, thread, turn, call)

    async def resume_recovery(
        self,
        thread: Thread,
        turn: Turn,
        calls: list[ToolCallContent],
        execute_calls: PendingCallExecutor,
    ) -> Turn | None:
        return await resume_action_execution(self._state, thread, turn, calls, execute_calls)

    def close(self) -> None:
        self._state.gateway.close()


def build_trusted_action_runtime(
    gateway: TrustedActionGateway | None,
    store: SessionStore,
    lock: LockFactory,
    validate_tool_contract: ToolContractValidator,
    fault: FaultInjector,
) -> TrustedActionSessionRuntime | None:
    """仅在产品组合提供Gateway时创建Session协调器。"""

    if gateway is None:
        return None
    return TrustedActionSessionRuntime(gateway, store, lock, validate_tool_contract, fault)


def action_definitions(
    runtime: TrustedActionSessionRuntime | None,
) -> tuple[ToolDescriptor, ...]:
    return runtime.definitions() if runtime is not None else ()


def action_owned(runtime: TrustedActionSessionRuntime | None, call: ToolCallContent) -> bool:
    return runtime is not None and runtime.owns(call)


def close_trusted_actions(runtime: TrustedActionSessionRuntime | None) -> None:
    if runtime is not None:
        runtime.close()


async def sync_trusted_action(
    runtime: TrustedActionSessionRuntime | None, thread_id: UUID, turn_id: UUID
) -> Turn | None:
    return await runtime.sync_decision(thread_id, turn_id) if runtime is not None else None


async def synchronize_waiting_action(
    runtime: TrustedActionSessionRuntime | None, thread: Thread, turn: Turn
) -> Turn:
    projected = await sync_trusted_action(runtime, thread.thread_id, turn.turn_id)
    return projected or turn


async def resume_after_approval(
    runtime: TrustedActionSessionRuntime | None,
    thread_id: UUID,
    turn_id: UUID,
    token: CancelToken,
    sync_legacy: TurnContinuation,
    continue_turn: TurnContinuation,
) -> Turn:
    """优先修复Router投影，再兼容Process投影，最后继续普通执行流。"""

    synchronized = await sync_trusted_action(runtime, thread_id, turn_id)
    if synchronized is None:
        synchronized = await sync_legacy(thread_id, turn_id, token)
    if synchronized is not None:
        return synchronized
    continued = await continue_turn(thread_id, turn_id, token)
    assert continued is not None
    return continued


async def resume_trusted_action(
    runtime: TrustedActionSessionRuntime | None,
    thread: Thread,
    turn: Turn,
    calls: list[ToolCallContent],
    execute_calls: PendingCallExecutor,
) -> Turn | None:
    if runtime is None:
        return None
    return await runtime.resume_recovery(thread, turn, calls, execute_calls)


def ensure_trusted_action_runtime(
    runtime: TrustedActionSessionRuntime | None, content: object
) -> None:
    if isinstance(content, TrustedActionApprovalRequestContent) and runtime is None:
        raise KernelError("trusted_action_not_enabled", "持久Trusted Action审批缺少原Gateway")


async def record_trusted_action_decision(
    runtime: TrustedActionSessionRuntime | None,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    item_id: UUID,
    content: ApprovalContent,
    decision: ApprovalDecision,
) -> Turn | None:
    if not isinstance(content, TrustedActionApprovalRequestContent):
        return None
    ensure_trusted_action_runtime(runtime, content)
    assert runtime is not None
    return await runtime.record_decision(thread, turn, call, item_id, content, decision)


async def prepare_trusted_action(
    runtime: TrustedActionSessionRuntime | None,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    token: CancelToken,
) -> tuple[ToolResultContent | None, TrustedActionApprovalRequestContent | None]:
    if runtime is None:
        raise KernelError("trusted_action_not_enabled", "Trusted Action Gateway不可用")
    prepared = await runtime.prepare(thread, turn, call, token)
    if isinstance(prepared, ToolResultContent):
        return prepared, None
    return None, prepared


async def execute_trusted_action(
    runtime: TrustedActionSessionRuntime | None,
    thread_id: UUID,
    turn_id: UUID,
    call: ToolCallContent,
    token: CancelToken,
) -> ToolResultContent:
    if runtime is None:
        raise KernelError("trusted_action_not_enabled", "Trusted Action Gateway不可用")
    return await runtime.execute(thread_id, turn_id, call, token)


async def recover_trusted_action(
    runtime: TrustedActionSessionRuntime | None,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
) -> ToolResultContent | None:
    if runtime is None:
        raise KernelError("trusted_action_not_enabled", "Trusted Action Gateway不可用")
    return await runtime.recover(thread, turn, call)
