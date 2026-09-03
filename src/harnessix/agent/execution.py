from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.approvals import execution_fingerprint, request_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, TurnStatus
from harnessix.agent.reducer import get_turn, pending_calls


@dataclass(frozen=True, slots=True)
class ToolExecutionScope:
    """Kernel 注入的调用归属；不是模型参数、文件系统能力或密码学凭证。"""

    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    workspace: str
    request_fingerprint: str

    @classmethod
    def for_pending_call(
        cls, thread: Thread, turn_id: UUID, call: ToolCallContent
    ) -> ToolExecutionScope:
        turn = get_turn(thread, turn_id)
        if (
            thread.active_turn_id != turn_id
            or turn.status != TurnStatus.EXECUTING_TOOLS
            or not any(pending == call for pending in pending_calls(turn))
        ):
            raise KernelError("tool_scope_mismatch", "工具调用不属于当前活跃执行作用域")
        return cls(
            thread_id=thread.thread_id,
            turn_id=turn_id,
            call_id=call.call_id,
            workspace=thread.workspace,
            request_fingerprint=request_fingerprint(thread, turn, call),
        )

    def validate_call(self, call: ToolCallContent) -> None:
        if self.call_id != call.call_id or self.request_fingerprint != execution_fingerprint(
            self.thread_id, self.turn_id, self.workspace, call
        ):
            raise KernelError("tool_scope_mismatch", "执行作用域与工具调用不匹配")
