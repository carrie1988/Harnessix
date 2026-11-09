"""按Tool名称把Agent调用分派给互不相交的Trusted Action Gateway。"""

from __future__ import annotations

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.agent.ports import TrustedActionGateway
from harnessix.domain.models import ApprovalDecision, ToolDescriptor


class CompositeAgentActionGateway:
    """让不同来源的Gateway共用Agent唯一入口；每个调用只属于一个来源。"""

    def __init__(self, *gateways: TrustedActionGateway) -> None:
        if not gateways:
            raise KernelError("trusted_action_gateway_invalid", "组合Gateway至少需要一个来源")
        owners: dict[str, TrustedActionGateway] = {}
        for gateway in gateways:
            for definition in gateway.definitions():
                if definition.name in owners:
                    raise KernelError("trusted_action_gateway_invalid", "Gateway Tool名称重复")
                owners[definition.name] = gateway
        self._gateways = gateways
        self._owners = owners

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            sorted(
                (item for gateway in self._gateways for item in gateway.definitions()),
                key=lambda item: item.name,
            )
        )

    async def prepare(
        self, thread: Thread, turn: Turn, call: ToolCallContent, cancel: CancelToken
    ) -> TrustedActionApprovalRequestContent | ToolResultContent:
        return await self._owner(call).prepare(thread, turn, call, cancel)

    def decide(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
        decision: ApprovalDecision,
    ) -> TrustedActionApprovalRequestContent:
        return self._owner(call).decide(thread, turn, call, approval, decision)

    def sync_decision(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
    ) -> TrustedActionApprovalRequestContent | None:
        return self._owner(call).sync_decision(thread, turn, call, approval)

    async def execute(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
        cancel: CancelToken,
    ) -> ToolResultContent:
        return await self._owner(call).execute(thread, turn, call, approval, cancel)

    async def recover(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent | None,
        cancel: CancelToken,
    ) -> ToolResultContent | None:
        return await self._owner(call).recover(thread, turn, call, approval, cancel)

    def close(self) -> None:
        for gateway in self._gateways:
            gateway.close()

    def _owner(self, call: ToolCallContent) -> TrustedActionGateway:
        try:
            return self._owners[call.tool]
        except KeyError:
            raise KernelError("trusted_action_not_registered", "调用不属于Gateway目录") from None
