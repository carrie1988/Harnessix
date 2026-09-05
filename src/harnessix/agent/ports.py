from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.domain.models import ApprovalDecision, ApprovalRecord, ToolDescriptor
from harnessix.patches.agent_bridge import PatchCallResult
from harnessix.patches.batch_agent_bridge import BatchCallResult
from harnessix.patches.batch_approval_contracts import ManagedPatchBatchApproval
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from harnessix.patches.managed_contracts import PatchRecord


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


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    """一次有界Action读取形成的Session状态，以及仅在终态存在的模型结果。"""

    state: ProcessActionStateContent
    result: ToolResultContent | None


class ProcessRuntime(Protocol):
    """显式受信的Agent/Action桥接端口；执行权仍只属于Action Worker。"""

    def definition(self) -> ToolDescriptor: ...

    async def prepare(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
        *,
        approval_id: UUID,
    ) -> ProcessApprovalRequestContent | ToolResultContent: ...

    async def decide(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        approval: ProcessApprovalRequestContent,
        decision: ApprovalDecision,
        cancel: CancelToken,
    ) -> ProcessApprovalRequestContent: ...

    async def sync_decision(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        approval: ProcessApprovalRequestContent,
        cancel: CancelToken,
    ) -> ProcessApprovalRequestContent | None: ...

    async def observe(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        approval: ProcessApprovalRequestContent,
        cancel: CancelToken,
    ) -> ProcessObservation: ...
