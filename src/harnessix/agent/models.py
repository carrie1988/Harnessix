from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from harnessix.agent.errors import AgentFailure as AgentFailure
from harnessix.agent.ids import new_id
from harnessix.agent.usage import (
    ModelAttempt,
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
)
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import (
    ApprovalRecord,
    ContractModel,
    EffectClass,
    TraceContext,
    utc_now,
)
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.batch_run_contracts import BatchExecutionResult
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from harnessix.patches.managed_contracts import PatchState
from harnessix.tools.contracts import Revision


class Budget(ContractModel):
    max_steps: int = Field(default=16, ge=1, le=1000)
    max_tokens: int = Field(default=100_000, ge=1)
    timeout_seconds: float = Field(default=120, gt=0, le=86400, allow_inf_nan=False)
    max_output_chars: int = Field(default=65536, ge=1, le=1_000_000)
    max_tool_calls_per_step: int = Field(default=32, ge=1, le=128)


class Usage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TurnStatus(StrEnum):
    ACCEPTED = "accepted"
    PREPARING_CONTEXT = "preparing_context"
    CALLING_MODEL = "calling_model"
    EXECUTING_TOOLS = "executing_tools"
    WAITING_APPROVAL = "waiting_approval"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_TURNS = frozenset(
    {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED}
)

TURN_TRANSITIONS = {
    TurnStatus.ACCEPTED: {TurnStatus.PREPARING_CONTEXT},
    TurnStatus.PREPARING_CONTEXT: {TurnStatus.CALLING_MODEL},
    TurnStatus.CALLING_MODEL: {TurnStatus.EXECUTING_TOOLS, TurnStatus.FINALIZING},
    TurnStatus.EXECUTING_TOOLS: {TurnStatus.PREPARING_CONTEXT, TurnStatus.WAITING_APPROVAL},
    TurnStatus.WAITING_APPROVAL: {TurnStatus.EXECUTING_TOOLS},
    TurnStatus.FINALIZING: {TurnStatus.COMPLETED},
    TurnStatus.CANCELLING: {TurnStatus.CANCELLED, TurnStatus.INTERRUPTED},
}


class ItemStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TextContent(ContractModel):
    kind: Literal["user_message", "assistant_message", "reasoning_summary"]
    text: str = Field(default="", max_length=1_000_000)


class ToolCallContent(ContractModel):
    kind: Literal["tool_call"] = "tool_call"
    call_id: UUID
    provider_call_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(min_length=1, max_length=256)
    tool_version: str = Field(min_length=1, max_length=128)
    effect_class: EffectClass
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    requires_approval: bool = False
    tool_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PatchEffect(ContractModel):
    """Session 私有的有界效果事实；模型不可注入，不是执行许可。"""

    workspace_id: UUID
    plan_id: UUID
    request_id: Revision
    approval_fingerprint: Revision
    state: PatchState
    origin: Literal["execution", "recovery"]


class PatchBatchEffect(ContractModel):
    """有界组效果证据；完整批准留在审批 Item，不回灌模型。"""

    workspace_id: UUID
    batch_id: UUID
    request_id: Revision
    approval_fingerprint: Revision
    origin: Literal["execution", "recovery"]
    execution: BatchExecutionResult | None = None

    @model_validator(mode="after")
    def bound_execution(self) -> Self:
        if self.execution is not None and (
            self.execution.run.phase != "finished"
            or self.execution.run.workspace_id != self.workspace_id
            or self.execution.run.batch_id != self.batch_id
        ):
            raise ValueError("组效果与终止运行身份不一致")
        if len(self.model_dump_json().encode()) > 8192:
            raise ValueError("私有组效果超过字节上限")
        return self


class ToolResultContent(ContractModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: UUID
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"]
    output: JsonValue = None
    error: AgentFailure | None = None
    action_id: UUID | None = None
    patch: PatchEffect | None = None
    patch_batch: PatchBatchEffect | None = None
    diff_artifact: ArtifactRef | None = None

    @model_serializer(mode="wrap")
    def serialize_result(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.patch is None:
            data.pop("patch", None)
        if self.patch_batch is None:
            data.pop("patch_batch", None)
        if self.diff_artifact is None:
            data.pop("diff_artifact", None)
        return data

    @model_validator(mode="after")
    def independent_effects(self) -> Self:
        if self.diff_artifact is not None and self.patch_batch is None:
            raise ValueError("差异效果引用必须附属于整组证据")
        if self.patch is not None and self.patch_batch is not None:
            raise ValueError("单文件和整组证据不能混用")
        return self


class ApprovalRequestContent(ContractModel):
    kind: Literal["approval_request"] = "approval_request"
    approval_id: UUID
    call_id: UUID
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ApprovalRecord | None = None
    policy_version: Literal["kernel-read-only/v1"] = "kernel-read-only/v1"


class PatchApprovalRequestContent(ContractModel):
    kind: Literal["patch_approval_request"] = "patch_approval_request"
    policy_version: Literal["kernel-managed-patch/v1"] = "kernel-managed-patch/v1"
    approval_id: UUID
    call_id: UUID
    plan: ManagedPatchCallPlan
    request_fingerprint: Revision
    decision: ApprovalRecord | None = None

    @model_validator(mode="after")
    def bound_plan(self) -> Self:
        if (
            self.call_id != self.plan.call_id
            or self.request_fingerprint != self.plan.approval_fingerprint
        ):
            raise ValueError("写审批必须绑定完整调用计划")
        return self


class PatchBatchApprovalRequestContent(ContractModel):
    kind: Literal["patch_batch_approval_request"] = "patch_batch_approval_request"
    policy_version: Literal["kernel-managed-patch-batch/v1"] = "kernel-managed-patch-batch/v1"
    approval_id: UUID
    call_id: UUID
    plan: ManagedPatchBatchCallPlan
    request_fingerprint: Revision
    decision: ApprovalRecord | None = None

    diff_artifact: ArtifactRef | None = None

    @model_serializer(mode="wrap")
    def serialize_request(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.diff_artifact is None:
            data.pop("diff_artifact", None)
        return data

    @model_validator(mode="after")
    def bound_plan(self) -> Self:
        if (
            self.call_id != self.plan.call_id
            or self.request_fingerprint != self.plan.approval_fingerprint
        ):
            raise ValueError("整组审批必须绑定完整调用计划")
        return self


ApprovalContent = (
    ApprovalRequestContent | PatchApprovalRequestContent | PatchBatchApprovalRequestContent
)


class PlanStep(ContractModel):
    step_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class PlanContent(ContractModel):
    kind: Literal["plan"] = "plan"
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=32)
    supersedes: UUID | None = None

    @model_validator(mode="after")
    def unique_steps(self) -> Self:
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("Plan 步骤 ID 必须唯一")
        if sum(step.status == "in_progress" for step in self.steps) > 1:
            raise ValueError("Plan 最多有一个进行中步骤")
        return self


class CompactionContent(ContractModel):
    kind: Literal["context_compaction"] = "context_compaction"
    source_item_ids: tuple[UUID, ...] = Field(min_length=1, max_length=4096)
    summary: str = Field(min_length=1, max_length=1_000_000)
    tokens_before: int = Field(ge=1)
    tokens_after: int = Field(ge=0)
    tokenizer: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_compression(self) -> Self:
        if len(set(self.source_item_ids)) != len(self.source_item_ids):
            raise ValueError("Compaction 来源 Item 不可重复")
        if self.tokens_after >= self.tokens_before:
            raise ValueError("Compaction 记录必须减少报告的 Token 数量")
        return self


class ErrorContent(ContractModel):
    kind: Literal["error"] = "error"
    failure: AgentFailure


ItemContent = Annotated[
    TextContent
    | ToolCallContent
    | ToolResultContent
    | ApprovalRequestContent
    | PatchApprovalRequestContent
    | PatchBatchApprovalRequestContent
    | PlanContent
    | CompactionContent
    | ErrorContent,
    Field(discriminator="kind"),
]


class Item(ContractModel):
    item_id: UUID
    status: ItemStatus
    content: ItemContent
    error: AgentFailure | None = None


class Turn(ContractModel):
    turn_id: UUID
    request_id: str
    request_fingerprint: str
    status: TurnStatus = TurnStatus.ACCEPTED
    budget: Budget
    trace_context: TraceContext | None = None
    items: tuple[Item, ...] = ()
    model_steps: int = 0
    usage_step: int = 0
    usage: Usage = Field(default_factory=Usage)
    model_attempts: tuple[ModelAttempt, ...] = ()
    error: AgentFailure | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @property
    def usage_is_complete(self) -> bool:
        # 旧 Provider 未上报内部尝试，不能据成功响应的总量断言账目完整。
        return (
            self.model_steps > 0
            and {a.step for a in self.model_attempts} == set(range(1, self.model_steps + 1))
            and all(
                a.status != "running" and a.usage.completeness == "complete"
                for a in self.model_attempts
            )
        )


class Thread(ContractModel):
    thread_id: UUID
    workspace: str
    sequence: int = 0
    active_turn_id: UUID | None = None
    turns: tuple[Turn, ...] = ()
    created_at: datetime
    updated_at: datetime


class ThreadCreated(ContractModel):
    type: Literal["thread_created"] = "thread_created"
    workspace: str = Field(min_length=1, max_length=4096)

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Workspace 必须使用绝对路径")
        return value


class TurnStarted(ContractModel):
    type: Literal["turn_started"] = "turn_started"
    request_id: str = Field(min_length=1, max_length=256)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget: Budget
    trace_context: TraceContext | None = None


class TurnStateChanged(ContractModel):
    type: Literal["turn_state_changed"] = "turn_state_changed"
    status: TurnStatus
    error: AgentFailure | None = None


class ItemStarted(ContractModel):
    type: Literal["item_started"] = "item_started"
    item_id: UUID
    content: ItemContent


class ItemFinished(ContractModel):
    type: Literal["item_finished"] = "item_finished"
    item_id: UUID
    status: Literal[ItemStatus.COMPLETED, ItemStatus.FAILED, ItemStatus.CANCELLED]
    content: ItemContent
    error: AgentFailure | None = None


class UsageRecorded(ContractModel):
    type: Literal["usage_recorded"] = "usage_recorded"
    step: int = Field(ge=1)
    usage: Usage


EventPayload = Annotated[
    ThreadCreated
    | TurnStarted
    | TurnStateChanged
    | ItemStarted
    | ItemFinished
    | UsageRecorded
    | ModelAttemptStarted
    | ModelUsageObserved
    | ModelAttemptFinished,
    Field(discriminator="type"),
]


class EventDraft(ContractModel):
    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8] = 8
    event_id: UUID = Field(default_factory=new_id)
    turn_id: UUID | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    payload: EventPayload

    @model_serializer(mode="wrap")
    def serialize_event(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.schema_version < 5 and isinstance(self.payload, ModelUsageObserved):
            data.get("payload", {}).pop("billing", None)
        if self.schema_version < 3:
            payload = data.get("payload", {})
            for failure in (payload.get("error"), payload.get("content", {}).get("error")):
                if isinstance(failure, dict):
                    failure.pop("category", None)
        if self.schema_version == 1:
            content = data.get("payload", {}).get("content", {})
            if content.get("kind") == "tool_call":
                # 旧事件导出仍符合冻结的 v1 Schema，不泄漏兼容读取时补上的 v2 默认字段。
                content.pop("requires_approval", None)
                content.pop("tool_fingerprint", None)
        return data

    @model_validator(mode="after")
    def legacy_event_boundary(self) -> Self:
        if self.schema_version < 8 and isinstance(self.payload, ItemStarted | ItemFinished):
            content = self.payload.content
            if isinstance(content, ToolResultContent | PatchBatchApprovalRequestContent):
                if content.diff_artifact is not None:
                    raise ValueError("差异归档引用需要 Agent Event v8")
        if self.schema_version < 7 and isinstance(self.payload, ItemStarted | ItemFinished):
            content = self.payload.content
            if isinstance(content, PatchBatchApprovalRequestContent) or (
                isinstance(content, ToolResultContent) and content.patch_batch is not None
            ):
                raise ValueError("整组审批与效果需要 Agent Event v7")
        if self.schema_version < 6 and isinstance(self.payload, ItemStarted | ItemFinished):
            content = self.payload.content
            if isinstance(content, PatchApprovalRequestContent) or (
                isinstance(content, ToolResultContent) and content.patch is not None
            ):
                raise ValueError("写审批和效果证据需要 Agent Event v6")
        if (
            self.schema_version < 5
            and isinstance(self.payload, ModelUsageObserved)
            and self.payload.billing is not None
        ):
            raise ValueError("响应计费元数据需要 Agent Event v5")
        if self.schema_version < 4 and isinstance(
            self.payload, ModelAttemptStarted | ModelUsageObserved | ModelAttemptFinished
        ):
            raise ValueError("模型尝试和用量观测需要 Agent Event v4")
        if self.schema_version < 3 and isinstance(self.payload, ItemStarted | ItemFinished):
            if isinstance(self.payload.content, PlanContent | CompactionContent | ErrorContent):
                raise ValueError("Plan/Compaction/Error Item 需要 Agent Event v3")
        if self.schema_version == 1:
            payload = self.payload
            if (
                isinstance(payload, TurnStateChanged)
                and payload.status == TurnStatus.WAITING_APPROVAL
            ):
                raise ValueError("审批状态需要 Agent Event v2")
            if isinstance(payload, ItemStarted | ItemFinished):
                content = payload.content
                if isinstance(content, ApprovalRequestContent) or (
                    isinstance(content, ToolCallContent)
                    and (content.requires_approval or content.tool_fingerprint is not None)
                ):
                    raise ValueError("审批契约需要 Agent Event v2")
        return self


class AgentEvent(EventDraft):
    thread_id: UUID
    sequence: int = Field(ge=1)


class ItemDelta(ContractModel):
    thread_id: UUID
    turn_id: UUID
    item_id: UUID
    model_step: int = Field(ge=1)
    stream_sequence: int = Field(ge=1)
    delta: str
