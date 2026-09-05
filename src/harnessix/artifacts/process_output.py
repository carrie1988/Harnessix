"""从已核对终态观察发布Process正文；归档失败不改写Action事实。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from harnessix.agent.approvals import approval_for, approval_matches
from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    AgentEvent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    ProcessApprovalRequestContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.agent.patching import inspection_scope
from harnessix.agent.ports import ProcessObservation, ProcessRuntime
from harnessix.agent.reducer import apply_event, get_turn, pending_calls
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore, records
from harnessix.domain.models import utc_now
from harnessix.processes.output_artifact import process_output_document
from harnessix.session.sqlite import SQLiteSessionStore


@dataclass(frozen=True, slots=True, repr=False)
class _Publication:
    ref: ArtifactRef
    body: bytes
    result: ToolResultContent


class SQLiteProcessArtifactPublisher:
    """同一Session事务保存正文、引用、终态观察和Tool Result。"""

    def __init__(
        self,
        artifacts: SQLiteArtifactStore,
        bridge: ProcessRuntime,
        *,
        workspace_scope: str,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", workspace_scope):
            raise KernelError("artifact_invalid", "Process Artifact工作区作用域不合法")
        self.artifacts = artifacts
        self._bridge = bridge
        self.workspace_scope = workspace_scope

    @property
    def session(self) -> SQLiteSessionStore:
        return self.artifacts.session

    @property
    def bridge(self) -> ProcessRuntime:
        return self._bridge

    @staticmethod
    def _result_item_id(drafts: Sequence[EventDraft], observation: ProcessObservation) -> UUID:
        assert observation.result is not None
        started = [
            draft.payload.item_id
            for draft in drafts
            if isinstance(draft.payload, ItemStarted)
            and draft.payload.content == observation.result
        ]
        finished = [
            draft.payload.item_id
            for draft in drafts
            if isinstance(draft.payload, ItemFinished)
            and draft.payload.content == observation.result
            and draft.payload.status == ItemStatus.COMPLETED
        ]
        if len(started) != 1 or started != finished:
            raise KernelError("artifact_invalid", "Process结果事件与终态观察不一致")
        return started[0]

    def _prepare(
        self,
        observation: ProcessObservation,
        drafts: Sequence[EventDraft],
        max_output_chars: int,
    ) -> tuple[_Publication, UUID] | None:
        if observation.result is None or observation.process is None:
            return None
        result = ToolResultContent.model_validate_json(observation.result.model_dump_json())
        if (
            result.process != observation.state.effect
            or result.action_id != observation.state.effect.action_id
            or not isinstance(result.output, dict)
            or "artifact" in result.output
        ):
            raise KernelError("artifact_invalid", "Process正文缺少匹配终态结果")
        document = process_output_document(observation.process)
        if document is None:
            return None
        body = document.to_jsonl()
        ref = ArtifactRef(
            artifact_id=new_id(),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            records=len(records(body)),
            complete=document.summary.complete,
            expires_at=utc_now() + timedelta(seconds=self.artifacts.policy.ttl_seconds),
        )
        published = result.model_copy(
            update={"output": {**result.output, "artifact": ref.model_dump(mode="json")}}
        )
        if (
            len(published.model_dump_json(include={"outcome", "output", "error", "diff_artifact"}))
            > max_output_chars
        ):
            return None
        return _Publication(ref=ref, body=body, result=published), self._result_item_id(
            drafts, observation
        )

    @staticmethod
    def _annotate(draft: EventDraft, item_id: UUID, result: ToolResultContent) -> EventDraft:
        payload = draft.payload
        if isinstance(payload, ItemStarted | ItemFinished) and payload.item_id == item_id:
            return draft.model_copy(
                update={"payload": payload.model_copy(update={"content": result})}
            )
        return draft

    @staticmethod
    def _validate_batch(
        thread: Thread,
        thread_id: UUID,
        batch: Sequence[EventDraft],
        expected_sequence: int,
    ) -> None:
        projected = thread
        for index, draft in enumerate(batch, 1):
            projected = apply_event(
                projected,
                AgentEvent(
                    **draft.model_dump(),
                    thread_id=thread_id,
                    sequence=expected_sequence + index,
                ),
            )

    @staticmethod
    def _validate_owner(
        thread: Thread,
        turn_id: UUID,
        call: ToolCallContent,
        observation: ProcessObservation,
    ) -> None:
        turn = get_turn(thread, turn_id)
        calls = pending_calls(turn)
        if not calls or calls[0] != call:
            raise KernelError("tool_scope_mismatch", "Process Artifact缺少原未结算调用")
        inspection_scope(thread, turn, call)
        approval = approval_for(turn, call)
        if (
            approval is None
            or approval.status != ItemStatus.COMPLETED
            or not isinstance(approval.content, ProcessApprovalRequestContent)
            or approval.content.decision is None
            or not approval_matches(thread, turn, call, approval.content)
            or approval.content.plan.action_id != observation.state.effect.action_id
            or approval.content.plan.action_fingerprint
            != observation.state.effect.action_fingerprint
            or approval.content.plan.approval_fingerprint
            != observation.state.effect.plan_fingerprint
        ):
            raise KernelError("approval_mismatch", "Process Artifact与原Action批准错绑")

    async def append(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        observation: ProcessObservation,
        drafts: Sequence[EventDraft],
        *,
        expected_sequence: int,
        max_output_chars: int,
    ) -> Thread:
        batch = self.session._freeze_batch(drafts)
        owner = self.session._runtime_owner_token
        if owner is None:
            raise KernelError("artifact_runtime_required", "Process Artifact需要活跃Session宿主")
        thread = await self.session.get_thread(thread_id)
        if thread.sequence != expected_sequence:
            raise KernelError("sequence_conflict", "Process Artifact发布时会话已变化")
        self._validate_owner(thread, turn_id, call, observation)
        self._validate_batch(thread, thread_id, batch, expected_sequence)
        try:
            prepared = self._prepare(observation, batch, max_output_chars)
        except (KernelError, ValueError):
            prepared = None
        if prepared is None:
            return await self.session.append(thread_id, batch, expected_sequence=expected_sequence)
        publication, item_id = prepared
        published = self.session._freeze_batch(
            tuple(self._annotate(draft, item_id, publication.result) for draft in batch)
        )
        try:
            async with self.session._connection() as database:
                await database.execute("BEGIN IMMEDIATE")
                current = await self.session._snapshot(database, thread_id)
                if current is None or current.sequence != expected_sequence:
                    raise KernelError("sequence_conflict", "Process Artifact准备期间会话已变化")
                self._validate_owner(current, turn_id, call, observation)
                await self.artifacts._check_quota(
                    database, thread_id, turn_id, publication.ref.size_bytes
                )
                ref = publication.ref
                await database.execute(
                    "INSERT INTO agent_artifacts VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, 'process_output')",
                    (
                        str(ref.artifact_id),
                        str(thread_id),
                        str(turn_id),
                        str(call.call_id),
                        self.workspace_scope,
                        ref.model_dump_json(),
                        ref.size_bytes,
                        ref.expires_at.isoformat(),
                        publication.body,
                    ),
                )
                self.artifacts._fault("process_output.after_insert")
                updated, _ = await self.session._append_in_transaction(
                    database,
                    thread_id,
                    published,
                    expected_sequence=expected_sequence,
                )
                self.artifacts._fault("process_output.before_commit")
                if self.session._runtime_owner_token is not owner:
                    raise KernelError("artifact_runtime_required", "发布期间Session宿主已关闭")
                await database.commit()
                self.artifacts._fault("process_output.after_commit")
                return updated
        except Exception:
            current = await self.session.get_thread(thread_id)
            if current.sequence != expected_sequence:
                events = await self.session.events(thread_id, after=expected_sequence)
                if len(events) >= len(published) and all(
                    EventDraft.model_validate(event.model_dump(exclude={"thread_id", "sequence"}))
                    == draft
                    for event, draft in zip(events[: len(published)], published, strict=True)
                ):
                    return current
                raise
            if self.session._runtime_owner_token is not owner:
                raise
            return await self.session.append(thread_id, batch, expected_sequence=expected_sequence)
