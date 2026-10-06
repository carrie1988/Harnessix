"""Agent调用到Trusted Action的稳定身份映射；不接受模型提供计划或幂等身份。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import CodingActionInvocation, TrustedToolBinding

if TYPE_CHECKING:
    from harnessix.trusted_actions.router import ActionPlanningContext, ResolvedAction


class AgentActionPreparer(Protocol):
    """注册宿主在首次规划前观察材料；只返回资源，不拥有策略或执行入口。"""

    async def prepare(
        self,
        invocation: CodingActionInvocation,
        arguments: BaseModel,
        context: ActionPlanningContext,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> ResolvedAction: ...


def build_agent_action_invocation(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    binding: TrustedToolBinding,
    *,
    requires_idempotency: bool,
) -> CodingActionInvocation:
    plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
    idempotency_key = None
    if requires_idempotency:
        idempotency_key = canonical_digest(
            {
                "spec_version": "harnessix.agent-trusted-action-idempotency/v1",
                "thread_id": str(thread.thread_id),
                "turn_id": str(turn.turn_id),
                "call_id": str(call.call_id),
                "tool_fingerprint": call.tool_fingerprint,
            }
        )
    return CodingActionInvocation(
        invocation_id=plan_id,
        source=binding.source,
        source_id=binding.source_id,
        tool=binding.tool,
        tool_version=binding.tool_version,
        tool_fingerprint=binding.tool_fingerprint,
        arguments=call.arguments,
        idempotency_key=idempotency_key,
    )
