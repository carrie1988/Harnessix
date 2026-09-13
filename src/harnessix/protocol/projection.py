"""公共Agent Protocol：把内部领域事实脱敏投影为公共协议视图。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from harnessix.agent.errors import AgentFailure
from harnessix.agent.models import (
    AgentEvent,
    ApprovalRequestContent,
    Budget,
    CompactionContent,
    ErrorContent,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    PlanContent,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    QuestionAnswerContent,
    QuestionRequestContent,
    TextContent,
    Thread,
    ThreadArchived,
    ThreadCreated,
    ThreadForked,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStarted,
    TurnStateChanged,
    Usage,
    UsageRecorded,
)
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import ApprovalRecord
from harnessix.protocol.contracts import (
    EventsReplayResult,
    ItemPublicEvent,
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicArtifactRef,
    PublicBudget,
    PublicCompactionContent,
    PublicErrorContent,
    PublicEvent,
    PublicEventData,
    PublicFailure,
    PublicItem,
    PublicItemContent,
    PublicPlanContent,
    PublicPlanStep,
    PublicProcessStateContent,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolResultContent,
    PublicUsage,
    ThreadArchiveView,
    ThreadPublicEvent,
    ThreadView,
    TurnStartedPublicEvent,
    TurnStatePublicEvent,
    TurnView,
    UsagePublicEvent,
)


def _failure(value: AgentFailure | None) -> PublicFailure | None:
    if value is None:
        return None
    return PublicFailure.model_validate(value.model_dump(mode="json"))


def _budget(value: Budget) -> PublicBudget:
    return PublicBudget.model_validate(value.model_dump(mode="json"))


def _usage(value: Usage) -> PublicUsage:
    data = value.model_dump(mode="json")
    return PublicUsage(**data, total_tokens=data["input_tokens"] + data["output_tokens"])


def _artifact(value: ArtifactRef | None) -> PublicArtifactRef | None:
    if value is None:
        return None
    return PublicArtifactRef.model_validate(value.model_dump())


def _decision(value: ApprovalRecord | None) -> PublicApprovalDecision | None:
    if value is None:
        return None
    return PublicApprovalDecision(
        outcome=value.outcome.value,
        actor=value.actor,
        reason=value.reason,
    )


def _approval(value: object) -> PublicApprovalRequestContent:
    approval_type: Literal["tool", "patch", "patch_batch", "process"]
    policy_version: str
    if isinstance(value, ApprovalRequestContent):
        approval_type = "tool"
        policy_version = value.policy_version
        diff_artifact = None
    elif isinstance(value, PatchApprovalRequestContent):
        approval_type = "patch"
        policy_version = value.policy_version
        diff_artifact = None
    elif isinstance(value, PatchBatchApprovalRequestContent):
        approval_type = "patch_batch"
        policy_version = value.policy_version
        diff_artifact = _artifact(value.diff_artifact)
    elif isinstance(value, ProcessApprovalRequestContent):
        approval_type = "process"
        policy_version = value.policy_version
        diff_artifact = None
    elif isinstance(value, TrustedActionApprovalRequestContent):
        approval_type = value.presentation
        policy_version = value.policy_version
        diff_artifact = _artifact(value.diff_artifact)
    else:
        raise TypeError("不支持的审批内容")
    return PublicApprovalRequestContent(
        approval_type=approval_type,
        approval_id=value.approval_id,
        call_id=value.call_id,
        request_fingerprint=value.request_fingerprint,
        policy_version=policy_version,
        decision=_decision(value.decision),
        diff_artifact=diff_artifact,
    )


def project_item(item: Item) -> PublicItem:
    content = item.content
    public: PublicItemContent
    if isinstance(content, TextContent):
        public = PublicTextContent(kind=content.kind, text=content.text)
    elif isinstance(content, ToolCallContent):
        public = PublicToolCallContent(
            call_id=content.call_id,
            tool=content.tool,
            tool_version=content.tool_version,
            effect_class=content.effect_class.value,
            arguments=content.arguments,
            requires_approval=content.requires_approval,
        )
    elif isinstance(content, ToolResultContent):
        public = PublicToolResultContent(
            call_id=content.call_id,
            outcome=content.outcome,
            output=content.output,
            error=_failure(content.error),
            action_id=content.action_id,
            diff_artifact=_artifact(content.diff_artifact),
        )
    elif isinstance(
        content,
        ApprovalRequestContent
        | PatchApprovalRequestContent
        | PatchBatchApprovalRequestContent
        | ProcessApprovalRequestContent
        | TrustedActionApprovalRequestContent,
    ):
        public = _approval(content)
    elif isinstance(content, QuestionRequestContent):
        public = PublicQuestionRequestContent(
            question_id=content.question_id,
            call_id=content.call_id,
            question=content.question,
            options=content.options,
        )
    elif isinstance(content, QuestionAnswerContent):
        public = PublicQuestionAnswerContent(
            question_id=content.question_id,
            call_id=content.call_id,
            answer=content.answer,
        )
    elif isinstance(content, ProcessActionStateContent):
        public = PublicProcessStateContent(
            call_id=content.call_id,
            action_id=content.effect.action_id,
            status=content.effect.status.value,
            origin=content.effect.origin,
        )
    elif isinstance(content, PlanContent):
        public = PublicPlanContent(
            steps=tuple(
                PublicPlanStep(
                    step_id=step.step_id,
                    description=step.description,
                    status=step.status,
                )
                for step in content.steps
            ),
            supersedes=content.supersedes,
        )
    elif isinstance(content, CompactionContent):
        public = PublicCompactionContent(
            source_items=len(content.source_item_ids),
            tokens_before=content.tokens_before,
            tokens_after=content.tokens_after,
            tokenizer=content.tokenizer,
        )
    elif isinstance(content, ErrorContent):
        failure = _failure(content.failure)
        assert failure is not None
        public = PublicErrorContent(failure=failure)
    else:
        raise TypeError(f"不支持的Item内容：{type(content).__name__}")
    return PublicItem(
        item_id=item.item_id,
        status=item.status.value,
        content=public,
        error=_failure(item.error),
    )


def project_turn(turn: Turn) -> TurnView:
    """把Turn及其Items投影为公共协议视图。"""
    return TurnView(
        turn_id=turn.turn_id,
        request_id=turn.request_id,
        retry_of_turn_id=turn.retry_of_turn_id,
        status=turn.status.value,
        budget=_budget(turn.budget),
        usage=_usage(turn.usage),
        model_steps=turn.model_steps,
        error=_failure(turn.error),
        created_at=turn.created_at,
        completed_at=turn.completed_at,
    )


def project_thread(thread: Thread) -> ThreadView:
    """把Thread聚合投影为公共协议视图。"""
    latest = project_turn(thread.turns[-1]) if thread.turns else None
    archive = (
        ThreadArchiveView(
            archived_at=thread.archive.archived_at,
            reason=thread.archive.reason,
        )
        if thread.archive is not None
        else None
    )
    return ThreadView(
        thread_id=thread.thread_id,
        workspace=thread.workspace,
        cursor=thread.sequence,
        active_turn_id=thread.active_turn_id,
        turn_count=len(thread.turns),
        latest_turn=latest,
        forked_from_thread_id=(
            thread.fork_snapshot.source_thread_id if thread.fork_snapshot is not None else None
        ),
        archive=archive,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def project_event(thread_id: UUID, event: AgentEvent) -> PublicEvent | None:
    """把内部Agent事件脱敏投影为公共协议事件。"""
    payload = event.payload
    data: PublicEventData
    if isinstance(payload, ThreadCreated):
        data = ThreadPublicEvent(type="thread_created", workspace=payload.workspace)
    elif isinstance(payload, ThreadForked):
        data = ThreadPublicEvent(
            type="thread_forked",
            workspace=payload.workspace,
            source_thread_id=payload.snapshot.source_thread_id,
        )
    elif isinstance(payload, ThreadArchived):
        data = ThreadPublicEvent(type="thread_archived", reason=payload.reason)
    elif isinstance(payload, TurnStarted):
        data = TurnStartedPublicEvent(
            request_id=payload.request_id,
            retry_of_turn_id=payload.retry_of_turn_id,
            budget=_budget(payload.budget),
        )
    elif isinstance(payload, TurnStateChanged):
        data = TurnStatePublicEvent(status=payload.status.value, error=_failure(payload.error))
    elif isinstance(payload, ItemStarted):
        data = ItemPublicEvent(
            type="item_started",
            item=project_item(
                Item(item_id=payload.item_id, status=ItemStatus.STARTED, content=payload.content)
            ),
        )
    elif isinstance(payload, ItemFinished):
        data = ItemPublicEvent(
            type="item_finished",
            item=project_item(
                Item(
                    item_id=payload.item_id,
                    status=payload.status,
                    content=payload.content,
                    error=payload.error,
                )
            ),
        )
    elif isinstance(payload, UsageRecorded):
        data = UsagePublicEvent(step=payload.step, usage=_usage(payload.usage))
    else:
        return None
    return PublicEvent(
        event_id=event.event_id,
        thread_id=thread_id,
        turn_id=event.turn_id,
        cursor=event.sequence,
        occurred_at=event.occurred_at,
        data=data,
    )


def project_replay(
    thread_id: UUID,
    events: list[AgentEvent],
    *,
    scanned_through: int,
    has_more: bool,
) -> EventsReplayResult:
    """将一个已确定内部扫描边界的页面投影为公共Replay页面。"""

    if events and events[-1].sequence > scanned_through:
        raise ValueError("扫描位置不能落后于页面事件")
    public = tuple(
        projected for event in events if (projected := project_event(thread_id, event)) is not None
    )
    return EventsReplayResult(
        thread_id=thread_id,
        events=public,
        scanned_through=scanned_through,
        has_more=has_more,
    )
