"""为真实整组会话事实附加报告；不接受调用方提供的正文或归档许可。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from harnessix.agent.approvals import approval_for, approval_matches
from harnessix.agent.batch_patching import validate_effect
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    AgentEvent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    PatchBatchApprovalRequestContent,
    Thread,
    ToolResultContent,
)
from harnessix.agent.patching import inspection_scope
from harnessix.agent.reducer import apply_event, get_turn, pending_calls
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore, records
from harnessix.domain.models import utc_now
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.diff_document_contracts import BatchDiffDocumentOptions
from harnessix.session.sqlite import SQLiteSessionStore


@dataclass(frozen=True, slots=True, repr=False)
class _Publication:
    item_id: UUID
    turn_id: UUID
    call_id: UUID
    scope: str
    purpose: str
    ref: ArtifactRef
    body: bytes


class SQLiteBatchDiffPublisher:
    """显式配置的 Session 事务扩展；桥接负责读取原镜像，不授予新写权限。"""

    def __init__(
        self,
        artifacts: SQLiteArtifactStore,
        bridge: ManagedPatchBatchBridge,
        *,
        options: BatchDiffDocumentOptions | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.bridge = bridge
        self.options = options or BatchDiffDocumentOptions()

    @property
    def session(self) -> SQLiteSessionStore:
        return self.artifacts.session

    async def _prepare(self, thread: Thread, draft: EventDraft) -> _Publication | None:
        payload = draft.payload
        if not isinstance(payload, ItemStarted) or draft.turn_id is None:
            return None
        content = payload.content
        if not isinstance(content, PatchBatchApprovalRequestContent | ToolResultContent):
            return None
        if isinstance(content, ToolResultContent) and content.patch_batch is None:
            return None
        if content.diff_artifact is not None:
            raise KernelError("artifact_invalid", "新差异引用必须由发布器生成")
        turn = get_turn(thread, draft.turn_id)
        call = next((c for c in pending_calls(turn) if c.call_id == content.call_id), None)
        if call is None:
            raise KernelError("tool_scope_mismatch", "报告没有原未结算调用")
        if isinstance(content, PatchBatchApprovalRequestContent):
            scope = ToolExecutionScope.for_pending_call(thread, turn.turn_id, call)
            if approval_for(turn, call) is not None or not approval_matches(
                thread, turn, call, content
            ):
                raise KernelError("approval_mismatch", "报告与原完整审批错绑")
            plan, decision, execution, view = content.plan, None, None, "plan"
        else:
            validate_effect(thread, turn, call, content)
            request = approval_for(turn, call)
            assert request is not None
            assert isinstance(request.content, PatchBatchApprovalRequestContent)
            assert content.patch_batch is not None
            plan = request.content.plan
            decision = request.content.decision
            execution = content.patch_batch.execution
            view = "effect"
            scope = inspection_scope(thread, turn, call)
        try:
            prepared = await self.bridge.diff(
                call,
                scope,
                plan,
                CancelToken(),
                view="plan" if view == "plan" else "effect",
                approval=decision,
                execution=execution,
                options=self.options,
            )
        except KernelError:
            # 报告不可用不抹去真实效果，也不补批、核对或更新已结算证据。
            return None
        body = prepared.document.to_jsonl()
        ref = ArtifactRef(
            artifact_id=new_id(),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            records=len(records(body)),
            complete=prepared.document.summary.complete,
            expires_at=utc_now() + timedelta(seconds=self.artifacts.policy.ttl_seconds),
        )
        if isinstance(content, ToolResultContent):
            public = content.model_copy(update={"diff_artifact": ref})
            if len(public.model_dump_json(exclude={"patch", "patch_batch"})) > (
                turn.budget.max_output_chars
            ):
                return None
        return _Publication(
            payload.item_id,
            turn.turn_id,
            call.call_id,
            plan.backend.manifest.workspace_scope,
            "batch_" + view,
            ref,
            body,
        )

    @staticmethod
    def _annotate(draft: EventDraft, refs: dict[UUID, ArtifactRef]) -> EventDraft:
        payload = draft.payload
        if isinstance(payload, ItemStarted | ItemFinished) and payload.item_id in refs:
            content = payload.content.model_copy(update={"diff_artifact": refs[payload.item_id]})
            return draft.model_copy(
                update={"payload": payload.model_copy(update={"content": content})}
            )
        return draft

    async def append(
        self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
    ) -> Thread:
        batch = self.session._freeze_batch(drafts)
        candidates = [
            d
            for d in batch
            if isinstance(d.payload, ItemStarted)
            and (
                isinstance(d.payload.content, PatchBatchApprovalRequestContent)
                or isinstance(d.payload.content, ToolResultContent)
                and d.payload.content.patch_batch is not None
            )
        ]
        if not candidates:
            return await self.session.append(thread_id, batch, expected_sequence=expected_sequence)
        owner = self.session._runtime_owner_token
        if owner is None:
            raise KernelError("artifact_runtime_required", "差异发布需要活跃 Session 宿主")
        thread = await self.session.get_thread(thread_id)
        if thread.sequence != expected_sequence:
            raise KernelError("sequence_conflict", "差异发布时会话已变化")
        # 先验证整个原事实批次，不能用报告准备掩盖无效授权或终态。
        projected = thread
        for index, draft in enumerate(batch, 1):
            projected = apply_event(
                projected,
                AgentEvent(
                    **draft.model_dump(), thread_id=thread_id, sequence=expected_sequence + index
                ),
            )
        publications = []
        for draft in candidates:
            publication = await self._prepare(thread, draft)
            if publication is not None:
                publications.append(publication)
        if not publications:
            return await self.session.append(thread_id, batch, expected_sequence=expected_sequence)
        refs: dict[UUID, ArtifactRef] = {}
        try:
            async with self.session._connection() as database:
                await database.execute("BEGIN IMMEDIATE")
                current = await self.session._snapshot(database, thread_id)
                if current is None or current.sequence != expected_sequence:
                    raise KernelError("sequence_conflict", "报告准备期间会话已变化")
                for publication in publications:
                    try:
                        await self.artifacts._check_quota(
                            database, thread_id, publication.turn_id, publication.ref.size_bytes
                        )
                    except KernelError as error:
                        if error.code != "artifact_quota_exceeded":
                            raise
                        continue
                    ref = publication.ref
                    await database.execute(
                        "INSERT INTO agent_artifacts VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?)",
                        (
                            str(ref.artifact_id),
                            str(thread_id),
                            str(publication.turn_id),
                            str(publication.call_id),
                            publication.scope,
                            ref.model_dump_json(),
                            ref.size_bytes,
                            ref.expires_at.isoformat(),
                            publication.body,
                            publication.purpose,
                        ),
                    )
                    refs[publication.item_id] = ref
                    self.artifacts._fault("batch_diff.after_insert")
                published = self.session._freeze_batch(
                    tuple(self._annotate(d, refs) for d in batch)
                )
                updated, _ = await self.session._append_in_transaction(
                    database, thread_id, published, expected_sequence=expected_sequence
                )
                self.artifacts._fault("batch_diff.before_commit")
                if self.session._runtime_owner_token is not owner:
                    raise KernelError("artifact_runtime_required", "发布期间 Session 宿主已关闭")
                await database.commit()
                self.artifacts._fault("batch_diff.after_commit")
                return updated
        except Exception:
            # 先按原事件身份确认提交结果；不能把丢确认当作可重放写入。
            current = await self.session.get_thread(thread_id)
            if current.sequence != expected_sequence:
                events = await self.session.events(thread_id, after=expected_sequence)
                published = tuple(self._annotate(d, refs) for d in batch)
                if len(events) >= len(published) and all(
                    EventDraft.model_validate(e.model_dump(exclude={"thread_id", "sequence"})) == d
                    for e, d in zip(events[: len(published)], published, strict=True)
                ):
                    return current
                raise
            if self.session._runtime_owner_token is not owner:
                raise
            # 正文事务已回滚，只结算原事实。若 Session 本身不可用，让其明确失败。
            return await self.session.append(thread_id, batch, expected_sequence=expected_sequence)
