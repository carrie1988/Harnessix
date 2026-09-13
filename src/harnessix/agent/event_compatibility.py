"""冻结Agent Event历史版本的新增字段与事件边界。"""

from __future__ import annotations

from harnessix.agent.models import (
    ApprovalRequestContent,
    CompactionContent,
    CompactionWindowActivated,
    ErrorContent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ModelHistoryPrepared,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    PlanContent,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    QuestionAnswerContent,
    QuestionRequestContent,
    ThreadArchived,
    ThreadForked,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
)
from harnessix.context.compaction_ledger_contracts import CompactionEvent
from harnessix.context.contracts import (
    ContextInspectionV2,
    ContextInspectionV3,
    ContextPrepared,
)
from harnessix.context.tool_result_contracts import ModelHistoryInspectionV2


def validate_event_boundary(event: EventDraft) -> None:
    """拒绝把新语义伪装成旧版本事件。"""

    _validate_v20_to_v18(event)
    _validate_v17_to_v15(event)
    _validate_v14_to_v9(event)
    _validate_v8_to_v4(event)
    _validate_v3_to_v1(event)


def _validate_v20_to_v18(event: EventDraft) -> None:
    if event.schema_version < 20 and isinstance(event.payload, ItemStarted | ItemFinished):
        content = event.payload.content
        if isinstance(content, TrustedActionApprovalRequestContent) or (
            isinstance(content, ToolResultContent) and content.trusted_action is not None
        ):
            raise ValueError("Trusted Action投影需要Agent Event v20")
    if event.schema_version < 19 and isinstance(event.payload, ItemStarted | ItemFinished):
        if isinstance(event.payload.content, QuestionRequestContent | QuestionAnswerContent):
            raise ValueError("持久提问需要Agent Event v19")
    if (
        event.schema_version < 19
        and isinstance(event.payload, TurnStateChanged)
        and (event.payload.reason != "normal" or event.payload.status == TurnStatus.WAITING_INPUT)
    ):
        raise ValueError("交互状态需要Agent Event v19")
    if (
        event.schema_version < 18
        and isinstance(event.payload, TurnStarted)
        and event.payload.execution_mode != "immediate"
    ):
        raise ValueError("延迟驱动Turn需要Agent Event v18")


def _validate_v17_to_v15(event: EventDraft) -> None:
    if (
        event.schema_version < 17
        and isinstance(event.payload, TurnStarted)
        and event.payload.retry_of_turn_id is not None
    ):
        raise ValueError("Turn Retry来源需要Agent Event v17")
    if event.schema_version < 16 and isinstance(event.payload, ThreadForked | ThreadArchived):
        raise ValueError("Thread生命周期事件需要Agent Event v16")
    if event.schema_version < 15 and (
        isinstance(event.payload, CompactionWindowActivated)
        or (
            isinstance(event.payload, ModelHistoryPrepared)
            and isinstance(event.payload.inspection, ModelHistoryInspectionV2)
        )
    ):
        raise ValueError("活动Compaction窗口需要Agent Event v15")


def _validate_v14_to_v9(event: EventDraft) -> None:
    if event.schema_version < 14 and isinstance(event.payload, CompactionEvent):
        raise ValueError("摘要尝试账本需要Agent Event v14")
    if event.schema_version < 13 and isinstance(event.payload, ModelHistoryPrepared):
        raise ValueError("Tool Result模型历史检查需要Agent Event v13")
    if (
        event.schema_version < 12
        and isinstance(event.payload, ContextPrepared)
        and isinstance(event.payload.inspection, ContextInspectionV3)
    ):
        raise ValueError("Context 多来源一致性快照需要 Agent Event v12")
    if (
        event.schema_version < 11
        and isinstance(event.payload, ContextPrepared)
        and isinstance(event.payload.inspection, ContextInspectionV2)
    ):
        raise ValueError("Context Source 快照需要 Agent Event v11")
    if event.schema_version < 10 and isinstance(event.payload, ContextPrepared):
        raise ValueError("Context 检查记录需要 Agent Event v10")
    if event.schema_version < 9:
        if (
            isinstance(event.payload, TurnStateChanged)
            and event.payload.status == TurnStatus.WAITING_ACTION
        ):
            raise ValueError("Process Action等待状态需要Agent Event v9")
        if isinstance(event.payload, ItemStarted | ItemFinished):
            content = event.payload.content
            if isinstance(content, ProcessApprovalRequestContent | ProcessActionStateContent) or (
                isinstance(content, ToolResultContent) and content.process is not None
            ):
                raise ValueError("Process Action投影需要Agent Event v9")


def _validate_v8_to_v4(event: EventDraft) -> None:
    if event.schema_version < 8 and isinstance(event.payload, ItemStarted | ItemFinished):
        content = event.payload.content
        if isinstance(content, ToolResultContent | PatchBatchApprovalRequestContent):
            if content.diff_artifact is not None:
                raise ValueError("差异归档引用需要 Agent Event v8")
    if event.schema_version < 7 and isinstance(event.payload, ItemStarted | ItemFinished):
        content = event.payload.content
        if isinstance(content, PatchBatchApprovalRequestContent) or (
            isinstance(content, ToolResultContent) and content.patch_batch is not None
        ):
            raise ValueError("整组审批与效果需要 Agent Event v7")
    if event.schema_version < 6 and isinstance(event.payload, ItemStarted | ItemFinished):
        content = event.payload.content
        if isinstance(content, PatchApprovalRequestContent) or (
            isinstance(content, ToolResultContent) and content.patch is not None
        ):
            raise ValueError("写审批和效果证据需要 Agent Event v6")
    if (
        event.schema_version < 5
        and isinstance(event.payload, ModelUsageObserved)
        and event.payload.billing is not None
    ):
        raise ValueError("响应计费元数据需要 Agent Event v5")
    if event.schema_version < 4 and isinstance(
        event.payload, ModelAttemptStarted | ModelUsageObserved | ModelAttemptFinished
    ):
        raise ValueError("模型尝试和用量观测需要 Agent Event v4")


def _validate_v3_to_v1(event: EventDraft) -> None:
    if event.schema_version < 3 and isinstance(event.payload, ItemStarted | ItemFinished):
        if isinstance(event.payload.content, PlanContent | CompactionContent | ErrorContent):
            raise ValueError("Plan/Compaction/Error Item 需要 Agent Event v3")
    if event.schema_version != 1:
        return
    payload = event.payload
    if isinstance(payload, TurnStateChanged) and payload.status == TurnStatus.WAITING_APPROVAL:
        raise ValueError("审批状态需要 Agent Event v2")
    if isinstance(payload, ItemStarted | ItemFinished):
        content = payload.content
        if isinstance(content, ApprovalRequestContent) or (
            isinstance(content, ToolCallContent)
            and (content.requires_approval or content.tool_fingerprint is not None)
        ):
            raise ValueError("审批契约需要 Agent Event v2")
