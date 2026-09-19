"""定义Agent可调用的Tool、Trusted Action与Patch端口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.domain.models import ApprovalDecision, ApprovalRecord, ToolDescriptor
from harnessix.patches.batch_approval_contracts import ManagedPatchBatchApproval
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from harnessix.patches.managed_contracts import PatchRecord

if TYPE_CHECKING:
    from harnessix.patches.agent_bridge import PatchCallResult
    from harnessix.patches.batch_agent_bridge import BatchCallResult


class ToolRuntime(Protocol):
    def definitions(self) -> tuple[ToolDescriptor, ...]: ...

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent: ...


class ScopedToolRuntime(Protocol):
    def definitions(self) -> tuple[ToolDescriptor, ...]: ...

    async def execute_scoped(
        self, call: ToolCallContent, scope: ToolExecutionScope, cancel: CancelToken
    ) -> ToolResultContent | ArtifactToolResult: ...


class NoTools:
    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return ()

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        return ToolResultContent(call_id=call.call_id, outcome="failed")


class TrustedActionGateway(Protocol):
    """Agent统一高风险Action端口；批准与执行权由实现背后的Router持有。"""

    def definitions(self) -> tuple[ToolDescriptor, ...]: ...

    async def prepare(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> TrustedActionApprovalRequestContent | ToolResultContent: ...

    def decide(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
        decision: ApprovalDecision,
    ) -> TrustedActionApprovalRequestContent: ...

    def sync_decision(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
    ) -> TrustedActionApprovalRequestContent | None: ...

    async def execute(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent,
        cancel: CancelToken,
    ) -> ToolResultContent: ...

    async def recover(
        self,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        approval: TrustedActionApprovalRequestContent | None,
        cancel: CancelToken,
    ) -> ToolResultContent | None: ...

    def close(self) -> None: ...


class PatchRuntime(Protocol):
    """显式受信 Patch 端口，不是任意写工具注册表。"""

    def definition(self) -> ToolDescriptor: ...

    async def prepare(
        self, call: ToolCallContent, scope: ToolExecutionScope, cancel: CancelToken
    ) -> ManagedPatchCallPlan: ...

    async def review(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchCallPlan,
        cancel: CancelToken,
        *,
        verify_source: bool = True,
    ) -> PatchRecord: ...

    async def execute(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchCallPlan,
        approval: ApprovalRecord,
        cancel: CancelToken,
    ) -> PatchCallResult: ...

    async def recover(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
        *,
        plan: ManagedPatchCallPlan | None = None,
        approval: ApprovalRecord | None = None,
    ) -> PatchCallResult: ...


class PatchBatchRuntime(Protocol):
    """独立整组准入，不能由单文件批准或通用写注册替代。"""

    def definition(self) -> ToolDescriptor: ...

    async def prepare(
        self, call: ToolCallContent, scope: ToolExecutionScope, cancel: CancelToken
    ) -> ManagedPatchBatchCallPlan: ...

    async def review(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchBatchCallPlan,
        cancel: CancelToken,
        *,
        verify_source: bool = True,
    ) -> ManagedPatchBatchApproval: ...

    async def execute(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchBatchCallPlan,
        approval: ApprovalRecord,
        cancel: CancelToken,
    ) -> BatchCallResult: ...

    async def recover(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
        *,
        plan: ManagedPatchBatchCallPlan | None = None,
        approval: ApprovalRecord | None = None,
    ) -> BatchCallResult: ...
