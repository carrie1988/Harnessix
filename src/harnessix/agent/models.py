"""持久Agent状态机：定义Agent事件、Thread、Turn和Item的版本化持久契约。"""

from __future__ import annotations

import hashlib
import json
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
from harnessix.agent.trusted_action_contracts import (
    TrustedActionApprovalRequestContent as TrustedActionApprovalRequestContent,
)
from harnessix.agent.trusted_action_contracts import TrustedActionEffect as TrustedActionEffect
from harnessix.agent.usage import (
    ModelAttempt,
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
)
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.context.compaction_ledger_contracts import (
    COMPACTION_OPEN,
    CompactionEvent,
    CompactionRecord,
)
from harnessix.context.contracts import (
    ContextInspectionRecord,
    ContextPrepared,
)
from harnessix.context.tool_result_contracts import (
    ModelHistoryInspectionRecord,
    ToolResultViewDecision,
    ToolResultViewPolicy,
)
from harnessix.domain.models import (
    ActionStatus,
    ApprovalOutcome,
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
from harnessix.processes.bridge_contracts import AgentProcessCallPlan
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
    WAITING_ACTION = "waiting_action"
    WAITING_INPUT = "waiting_input"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TurnStatusV18(StrEnum):
    # 已发布辅助报告使用的冻结状态集合；新状态必须发布新报告版本。
    ACCEPTED = "accepted"
    PREPARING_CONTEXT = "preparing_context"
    CALLING_MODEL = "calling_model"
    EXECUTING_TOOLS = "executing_tools"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_ACTION = "waiting_action"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


# 保持历史JSON Schema的定义名；Python符号名明确标识冻结边界。
TurnStatusV18.__name__ = "TurnStatus"
TurnStatusV18.__qualname__ = "TurnStatus"


TERMINAL_TURNS = frozenset(
    {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED}
)

TURN_TRANSITIONS = {
    TurnStatus.ACCEPTED: {TurnStatus.PREPARING_CONTEXT},
    TurnStatus.PREPARING_CONTEXT: {TurnStatus.CALLING_MODEL},
    TurnStatus.CALLING_MODEL: {
        TurnStatus.PREPARING_CONTEXT,
        TurnStatus.EXECUTING_TOOLS,
        TurnStatus.FINALIZING,
    },
    TurnStatus.EXECUTING_TOOLS: {
        TurnStatus.PREPARING_CONTEXT,
        TurnStatus.WAITING_APPROVAL,
        TurnStatus.WAITING_INPUT,
    },
    TurnStatus.WAITING_APPROVAL: {TurnStatus.EXECUTING_TOOLS, TurnStatus.WAITING_ACTION},
    TurnStatus.WAITING_ACTION: {TurnStatus.EXECUTING_TOOLS},
    TurnStatus.WAITING_INPUT: {TurnStatus.EXECUTING_TOOLS},
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


class AskUserInput(ContractModel):
    question: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("options")
    @classmethod
    def valid_options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not option or len(option) > 500 for option in value
        ):
            raise ValueError("提问选项必须唯一且长度有效")
        return value


class QuestionRequestContent(ContractModel):
    kind: Literal["question_request"] = "question_request"
    question_id: UUID
    call_id: UUID
    question: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("options")
    @classmethod
    def valid_options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return AskUserInput.valid_options(value)


class QuestionAnswerContent(ContractModel):
    kind: Literal["question_answer"] = "question_answer"
    question_id: UUID
    call_id: UUID
    answer: str = Field(min_length=1, max_length=4000)


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


PROCESS_WAITING_STATUSES = frozenset(
    {ActionStatus.READY, ActionStatus.LEASED, ActionStatus.RUNNING, ActionStatus.RECONCILING}
)
PROCESS_RESOLVED_STATUSES = frozenset(
    {
        ActionStatus.DENIED,
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.UNKNOWN,
        ActionStatus.MANUAL_INTERVENTION,
    }
)


class ProcessActionEffect(ContractModel):
    """Session私有Action投影；不包含argv、输出或执行许可。"""

    plan_fingerprint: Revision
    action_id: UUID
    action_fingerprint: Revision
    status: ActionStatus
    result_fingerprint: Revision | None = None
    origin: Literal["execution", "recovery"]

    @model_validator(mode="after")
    def resolved_result(self) -> Self:
        if self.status not in PROCESS_WAITING_STATUSES | PROCESS_RESOLVED_STATUSES:
            raise ValueError("Process Action状态不可投影到等待边界")
        if self.status in PROCESS_RESOLVED_STATUSES and self.result_fingerprint is None:
            raise ValueError("Process终止投影缺少Action Result指纹")
        return self


class ProcessActionStateContent(ContractModel):
    kind: Literal["process_action_state"] = "process_action_state"
    call_id: UUID
    effect: ProcessActionEffect


class ToolResultContent(ContractModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: UUID
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"]
    output: JsonValue = None
    error: AgentFailure | None = None
    action_id: UUID | None = None
    patch: PatchEffect | None = None
    patch_batch: PatchBatchEffect | None = None
    process: ProcessActionEffect | None = None
    trusted_action: TrustedActionEffect | None = None
    diff_artifact: ArtifactRef | None = None

    @model_serializer(mode="wrap")
    def serialize_result(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.patch is None:
            data.pop("patch", None)
        if self.patch_batch is None:
            data.pop("patch_batch", None)
        if self.process is None:
            data.pop("process", None)
        if self.trusted_action is None:
            data.pop("trusted_action", None)
        if self.diff_artifact is None:
            data.pop("diff_artifact", None)
        return data

    @model_validator(mode="after")
    def independent_effects(self) -> Self:
        if (
            self.diff_artifact is not None
            and self.patch_batch is None
            and self.trusted_action is None
        ):
            raise ValueError("差异效果引用必须附属于整组证据")
        effects = (self.patch, self.patch_batch, self.process, self.trusted_action)
        if sum(effect is not None for effect in effects) > 1:
            raise ValueError("旧专用效果与Trusted Action效果不能混用")
        if self.process is not None and self.action_id != self.process.action_id:
            raise ValueError("Process效果与Tool Result Action ID不一致")
        if self.trusted_action is not None:
            expected = {
                "succeeded": "succeeded",
                "failed": "failed",
                "unknown": "unknown",
                "manual_intervention": "unknown",
            }[self.trusted_action.state]
            if self.action_id != self.trusted_action.plan_id or self.outcome != expected:
                raise ValueError("Trusted Action效果与Tool Result终态不一致")
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


class ProcessApprovalRequestContent(ContractModel):
    kind: Literal["process_approval_request"] = "process_approval_request"
    policy_version: Literal["kernel-process-action/v1"] = "kernel-process-action/v1"
    approval_id: UUID
    call_id: UUID
    plan: AgentProcessCallPlan
    request_fingerprint: Revision
    action_status: ActionStatus = ActionStatus.PENDING_APPROVAL
    decision: ApprovalRecord | None = None

    @model_validator(mode="after")
    def bound_action(self) -> Self:
        if (
            self.call_id != self.plan.call_id
            or self.request_fingerprint != self.plan.approval_fingerprint
        ):
            raise ValueError("Process审批必须绑定完整Action计划")
        if self.decision is None:
            if self.action_status is not ActionStatus.PENDING_APPROVAL:
                raise ValueError("未决定的Process审批只能投影PENDING_APPROVAL")
            return self
        if self.decision.request_fingerprint != self.plan.action_fingerprint:
            raise ValueError("Process决定必须来自原Action审批事实")
        if self.decision.outcome is ApprovalOutcome.REJECTED:
            if self.action_status is not ActionStatus.DENIED:
                raise ValueError("拒绝决定必须投影DENIED Action")
        elif self.action_status not in PROCESS_WAITING_STATUSES | (
            PROCESS_RESOLVED_STATUSES - {ActionStatus.DENIED}
        ):
            raise ValueError("批准决定与Process Action状态不一致")
        return self


ApprovalContent = (
    ApprovalRequestContent
    | PatchApprovalRequestContent
    | PatchBatchApprovalRequestContent
    | ProcessApprovalRequestContent
    | TrustedActionApprovalRequestContent
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
    | ProcessApprovalRequestContent
    | TrustedActionApprovalRequestContent
    | ProcessActionStateContent
    | QuestionRequestContent
    | QuestionAnswerContent
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


class ForkArtifactOwner(ContractModel):
    artifact_id: UUID
    owner_thread_id: UUID


class ThreadForkSnapshot(ContractModel):
    """Fork继承的只读历史；不属于子Thread的可执行Turn。"""

    spec_version: Literal["harnessix.thread-fork/v1"] = "harnessix.thread-fork/v1"
    authority: Literal["none"] = "none"
    request_id: str = Field(min_length=1, max_length=256)
    source_thread_id: UUID
    source_sequence: int = Field(ge=1, strict=True)
    through_turn_id: UUID | None = None
    source_compaction_window_id: UUID | None = None
    source_thread_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_result_view_policy: ToolResultViewPolicy
    items: tuple[Item, ...] = Field(default_factory=tuple, max_length=8192)
    tool_result_view_decisions: tuple[ToolResultViewDecision, ...] = Field(
        default_factory=tuple, max_length=8192
    )
    artifact_owners: tuple[ForkArtifactOwner, ...] = Field(default_factory=tuple, max_length=16384)

    @model_validator(mode="after")
    def safe_history(self) -> Self:
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("Fork历史Item身份重复")
        calls: set[UUID] = set()
        settled: set[UUID] = set()
        result_ids: set[UUID] = set()
        for item in self.items:
            if item.status != ItemStatus.COMPLETED or not isinstance(
                item.content, TextContent | ToolCallContent | ToolResultContent
            ):
                raise ValueError("Fork只能继承已完成的模型历史Item")
            if isinstance(item.content, ToolCallContent):
                if item.content.call_id in calls:
                    raise ValueError("Fork历史Tool Call身份重复")
                calls.add(item.content.call_id)
            elif isinstance(item.content, ToolResultContent):
                if item.content.call_id not in calls or item.content.call_id in settled:
                    raise ValueError("Fork历史Tool Result缺少唯一前置调用")
                settled.add(item.content.call_id)
                result_ids.add(item.item_id)
        if calls != settled:
            raise ValueError("Fork历史不能继承未结算Tool Call")
        decisions = self.tool_result_view_decisions
        if (
            len({decision.item_id for decision in decisions}) != len(decisions)
            or {decision.item_id for decision in decisions} != result_ids
        ):
            raise ValueError("Fork历史必须为每个Tool Result冻结唯一模型视图")
        owner_ids = [owner.artifact_id for owner in self.artifact_owners]
        referenced_ids = {
            binding.artifact.artifact_id
            for decision in decisions
            for binding in decision.references
        }
        if len(set(owner_ids)) != len(owner_ids) or set(owner_ids) != referenced_ids:
            raise ValueError("Fork历史Artifact所有者必须完整且唯一")
        encoded = json.dumps(
            [item.model_dump(mode="json") for item in self.items],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > 8_388_608:
            raise ValueError("Fork历史超过8 MiB持久化上限")
        if hashlib.sha256(encoded).hexdigest() != self.source_history_sha256:
            raise ValueError("Fork历史摘要与Item不一致")
        return self


class ThreadArchiveRecord(ContractModel):
    spec_version: Literal["harnessix.thread-archive/v1"] = "harnessix.thread-archive/v1"
    archived_event_sequence: int = Field(ge=2, strict=True)
    archived_at: AwareDatetime
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class CompactionWindow(ContractModel):
    spec_version: Literal["harnessix.compaction-window/v1"] = "harnessix.compaction-window/v1"
    window_id: UUID
    compaction_id: UUID
    previous_window_id: UUID | None = None
    source_finished_event_sequence: int = Field(ge=3, strict=True)
    activated_event_sequence: int = Field(ge=4, strict=True)
    model_step: int = Field(ge=1, le=1000, strict=True)
    history_item_ids: tuple[UUID, ...] = Field(min_length=2, max_length=8192)
    history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    history_tokens: int = Field(ge=1, le=8_388_608, strict=True)
    raw_history_items: int = Field(ge=1, le=8_388_608, strict=True)
    raw_history_last_item_id: UUID
    raw_history_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: AwareDatetime

    @model_validator(mode="after")
    def coherent_window(self) -> Self:
        if self.activated_event_sequence != self.source_finished_event_sequence + 1:
            raise ValueError("窗口发布事件必须紧邻候选结束事件")
        if len(set(self.history_item_ids)) != len(self.history_item_ids):
            raise ValueError("活动窗口Item身份重复")
        return self


class Turn(ContractModel):
    turn_id: UUID
    request_id: str
    request_fingerprint: str
    retry_of_turn_id: UUID | None = None
    execution_mode: Literal["immediate", "deferred"] = "immediate"
    status: TurnStatus = TurnStatus.ACCEPTED
    budget: Budget
    trace_context: TraceContext | None = None
    items: tuple[Item, ...] = ()
    model_steps: int = 0
    usage_step: int = 0
    usage: Usage = Field(default_factory=Usage)
    model_attempts: tuple[ModelAttempt, ...] = ()
    compactions: tuple[CompactionRecord, ...] = Field(default_factory=tuple, max_length=1000)
    context_inspections: tuple[ContextInspectionRecord, ...] = ()
    tool_result_view_decisions: tuple[ToolResultViewDecision, ...] = ()
    model_history_inspections: tuple[ModelHistoryInspectionRecord, ...] = ()
    error: AgentFailure | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @property
    def accounted_attempts(self) -> tuple[ModelAttempt, ...]:
        return (
            *self.model_attempts,
            *(c.attempt for c in self.compactions if c.attempt is not None),
        )

    @property
    def usage_is_complete(self) -> bool:
        # 旧 Provider 未上报内部尝试，不能据成功响应的总量断言账目完整。
        return (
            self.model_steps > 0
            and {a.step for a in self.model_attempts} == set(range(1, self.model_steps + 1))
            and all(
                a.status != "running" and a.usage.completeness == "complete"
                for a in self.accounted_attempts
            )
            and all(c.status not in COMPACTION_OPEN for c in self.compactions)
        )


class Thread(ContractModel):
    thread_id: UUID
    workspace: str
    sequence: int = 0
    active_turn_id: UUID | None = None
    turns: tuple[Turn, ...] = ()
    compaction_windows: tuple[CompactionWindow, ...] = Field(default_factory=tuple, max_length=1000)
    active_compaction_window_id: UUID | None = None
    fork_snapshot: ThreadForkSnapshot | None = None
    archive: ThreadArchiveRecord | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def linear_compaction_windows(self) -> Self:
        ids: set[UUID] = set()
        compactions: set[UUID] = set()
        previous: UUID | None = None
        for window in self.compaction_windows:
            if (
                window.window_id in ids
                or window.compaction_id in compactions
                or window.previous_window_id != previous
            ):
                raise ValueError("Compaction窗口身份重复或链路不连续")
            ids.add(window.window_id)
            compactions.add(window.compaction_id)
            previous = window.window_id
        if self.active_compaction_window_id != previous:
            raise ValueError("活动Compaction窗口必须指向线性链尾")
        if self.fork_snapshot is not None and self.fork_snapshot.source_thread_id == self.thread_id:
            raise ValueError("Thread不能Fork自身")
        if self.archive is not None and self.archive.archived_event_sequence != self.sequence:
            raise ValueError("归档记录必须位于Thread当前序号")
        for index, turn in enumerate(self.turns):
            if turn.retry_of_turn_id is None:
                continue
            if index == 0 or self.turns[index - 1].turn_id != turn.retry_of_turn_id:
                raise ValueError("Retry来源必须是直接前序Turn")
            source = self.turns[index - 1]
            if source.status not in {
                TurnStatus.FAILED,
                TurnStatus.CANCELLED,
                TurnStatus.INTERRUPTED,
            } or any(
                isinstance(item.content, ToolResultContent) and item.content.outcome == "unknown"
                for item in source.items
            ):
                raise ValueError("Retry来源状态或工具效果不安全")
        return self


class ThreadCreated(ContractModel):
    type: Literal["thread_created"] = "thread_created"
    workspace: str = Field(min_length=1, max_length=4096)

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("Workspace 必须使用绝对路径")
        return value


class ThreadForked(ContractModel):
    type: Literal["thread_forked"] = "thread_forked"
    workspace: str = Field(min_length=1, max_length=4096)
    snapshot: ThreadForkSnapshot

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("Workspace 必须使用绝对路径")
        return value


class ThreadArchived(ContractModel):
    type: Literal["thread_archived"] = "thread_archived"
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class TurnStarted(ContractModel):
    type: Literal["turn_started"] = "turn_started"
    request_id: str = Field(min_length=1, max_length=256)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_of_turn_id: UUID | None = None
    execution_mode: Literal["immediate", "deferred"] = "immediate"
    budget: Budget
    trace_context: TraceContext | None = None


class TurnStateChanged(ContractModel):
    type: Literal["turn_state_changed"] = "turn_state_changed"
    status: TurnStatus
    error: AgentFailure | None = None
    reason: Literal["normal", "context_overflow", "steering"] = "normal"


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


class ModelHistoryPrepared(ContractModel):
    type: Literal["model_history_prepared"] = "model_history_prepared"
    inspection: ModelHistoryInspectionRecord
    decisions: tuple[ToolResultViewDecision, ...] = Field(default_factory=tuple, max_length=8192)


class CompactionWindowActivated(ContractModel):
    type: Literal["compaction_window_activated"] = "compaction_window_activated"
    window: CompactionWindow


EventPayload = Annotated[
    ThreadCreated
    | ThreadForked
    | ThreadArchived
    | TurnStarted
    | TurnStateChanged
    | ItemStarted
    | ItemFinished
    | UsageRecorded
    | ModelAttemptStarted
    | ModelUsageObserved
    | ModelAttemptFinished
    | ModelHistoryPrepared
    | CompactionEvent
    | CompactionWindowActivated
    | ContextPrepared,
    Field(discriminator="type"),
]


class EventDraft(ContractModel):
    schema_version: Literal[
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20
    ] = 20
    event_id: UUID = Field(default_factory=new_id)
    turn_id: UUID | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    payload: EventPayload

    @model_serializer(mode="wrap")
    def serialize_event(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.schema_version < 19 and isinstance(self.payload, TurnStateChanged):
            data.get("payload", {}).pop("reason", None)
        if self.schema_version < 18 and isinstance(self.payload, TurnStarted):
            data.get("payload", {}).pop("execution_mode", None)
        if self.schema_version < 17 and isinstance(self.payload, TurnStarted):
            data.get("payload", {}).pop("retry_of_turn_id", None)
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
        from harnessix.agent.event_compatibility import validate_event_boundary

        validate_event_boundary(self)
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
