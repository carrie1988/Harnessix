"""把Agent持久调用映射到唯一Trusted Action计划、审批和终态。"""

from __future__ import annotations

from collections.abc import Mapping

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.domain.models import ApprovalDecision, ToolDescriptor
from harnessix.trusted_actions.agent_gateway_output import (
    TrustedActionOutputProvider as TrustedActionOutputProvider,
)
from harnessix.trusted_actions.agent_gateway_support import (
    PlanningContextFactory,
    TrustedActionPresentation,
    build_gateway_state,
    decide_action,
    execute_action,
    gateway_definitions,
    prepare_action,
    recover_action,
    sync_action_decision,
)
from harnessix.trusted_actions.agent_gateway_support import (
    TrustedActionReviewProvider as TrustedActionReviewProvider,
)
from harnessix.trusted_actions.router import TrustedActionRouter


class RouterBackedAgentActionGateway:
    """Agent唯一Trusted Action入口；Session只保存Router事实的交互投影。"""

    def __init__(
        self,
        router: TrustedActionRouter,
        definitions: tuple[ToolDescriptor, ...],
        context: PlanningContextFactory,
        *,
        source: str = "builtin",
        source_id: str = "harnessix.product",
        presentations: Mapping[str, TrustedActionPresentation] | None = None,
        reviews: TrustedActionReviewProvider | None = None,
        outputs: Mapping[str, TrustedActionOutputProvider] | None = None,
    ) -> None:
        self._state = build_gateway_state(
            router,
            definitions,
            context,
            source=source,
            source_id=source_id,
            presentations=presentations,
            reviews=reviews,
            outputs=outputs,
        )
        self._closed = False

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        self._ensure_open()
        return gateway_definitions(self._state)

    async def prepare(
        self, thread: Thread, turn: Turn, call: ToolCallContent, cancel: CancelToken
    ) -> TrustedActionApprovalRequestContent | ToolResultContent:
        self._ensure_open()
        return await prepare_action(self._state, thread, turn, call, cancel)

    def decide(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
        decision: ApprovalDecision,
    ) -> TrustedActionApprovalRequestContent:
        self._ensure_open()
        return decide_action(self._state, thread, turn, call, approval, decision)

    def sync_decision(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
    ) -> TrustedActionApprovalRequestContent | None:
        self._ensure_open()
        return sync_action_decision(self._state, thread, turn, call, approval)

    async def execute(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
        cancel: CancelToken,
    ) -> ToolResultContent:
        self._ensure_open()
        return await execute_action(self._state, thread, turn, call, approval, cancel)

    async def recover(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent | None,
        cancel: CancelToken,
    ) -> ToolResultContent | None:
        self._ensure_open()
        return await recover_action(self._state, thread, turn, call, approval, cancel)

    def close(self) -> None:
        """关闭能力视图；底层Router及Stores仍由产品组合Owner关闭。"""

        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise KernelError("trusted_action_gateway_closed", "Trusted Action Gateway已关闭")
