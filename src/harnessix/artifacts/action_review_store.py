"""Action Review Artifact的查询优先发布与Session引用校验。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import aiosqlite

from harnessix.agent.approvals import approval_for
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    ToolCallContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.agent.reducer import get_turn
from harnessix.artifacts.contracts import ArtifactPolicy, ArtifactRef
from harnessix.domain.models import EffectClass, utc_now
from harnessix.session.sqlite import SQLiteSessionStore

QuotaCheck = Callable[[aiosqlite.Connection, UUID, UUID, int], Awaitable[None]]
FaultInjector = Callable[[str], None]


async def publish_action_review(
    session: SQLiteSessionStore,
    policy: ArtifactPolicy,
    fault: FaultInjector,
    check_quota: QuotaCheck,
    thread_id: UUID,
    turn_id: UUID,
    call: ToolCallContent,
    body: bytes,
    *,
    artifact_id: UUID,
    workspace_scope: str,
    expected_sequence: int,
    record_count: int,
) -> ArtifactRef:
    """发布或查询同一Review收据；提交确认丢失时不得生成新身份。"""

    owner = session._runtime_owner_token
    if owner is None:
        raise KernelError("artifact_runtime_required", "Action Review发布需要活跃Session宿主")
    if (
        not call.requires_approval
        or call.effect_class is EffectClass.READ_ONLY
        or not re.fullmatch(r"[0-9a-f]{64}", workspace_scope)
    ):
        raise KernelError("artifact_invalid", "Action Review与写调用或Workspace不匹配")
    candidate = ArtifactRef(
        artifact_id=artifact_id,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        records=record_count,
        complete=True,
        expires_at=utc_now() + timedelta(seconds=policy.ttl_seconds),
    )
    try:
        async with session._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            existing = await _stored_review(database, call.call_id)
            if existing is not None:
                matched = matching_action_review(
                    existing,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    call_id=call.call_id,
                    artifact_id=artifact_id,
                    workspace_scope=workspace_scope,
                    body=body,
                    record_count=record_count,
                )
                await database.commit()
                return matched
            thread = await session._snapshot(database, thread_id)
            if thread is None or thread.sequence != expected_sequence:
                raise KernelError("sequence_conflict", "Action Review发布时会话已变化")
            ToolExecutionScope.for_pending_call(thread, turn_id, call)
            if approval_for(get_turn(thread, turn_id), call) is not None:
                raise KernelError("approval_mismatch", "Action Review不能替换已有审批")
            await check_quota(database, thread_id, turn_id, candidate.size_bytes)
            await database.execute(
                "INSERT INTO agent_artifacts VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, 'action_review')",
                (
                    str(candidate.artifact_id),
                    str(thread_id),
                    str(turn_id),
                    str(call.call_id),
                    workspace_scope,
                    candidate.model_dump_json(),
                    candidate.size_bytes,
                    candidate.expires_at.isoformat(),
                    body,
                ),
            )
            fault("action_review.after_insert")
            if session._runtime_owner_token is not owner:
                raise KernelError("artifact_runtime_required", "Action Review发布期间宿主已关闭")
            await database.commit()
            fault("action_review.after_commit")
            return candidate
    except Exception:
        # COMMIT确认丢失时只查询原身份；不生成新Artifact或刷新TTL。
        async with session._connection() as database:
            existing = await _stored_review(database, call.call_id)
            if existing is not None:
                return matching_action_review(
                    existing,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    call_id=call.call_id,
                    artifact_id=artifact_id,
                    workspace_scope=workspace_scope,
                    body=body,
                    record_count=record_count,
                )
        raise


async def _stored_review(
    database: aiosqlite.Connection,
    call_id: UUID,
) -> aiosqlite.Row | None:
    cursor = await database.execute(
        "SELECT * FROM agent_artifacts WHERE call_id = ? AND purpose = 'action_review'",
        (str(call_id),),
    )
    return await cursor.fetchone()


def matching_action_review(
    row: aiosqlite.Row,
    *,
    thread_id: UUID,
    turn_id: UUID,
    call_id: UUID,
    artifact_id: UUID,
    workspace_scope: str,
    body: bytes,
    record_count: int,
) -> ArtifactRef:
    """精确匹配稳定身份、作用域、正文和原始TTL。"""

    try:
        ref = ArtifactRef.model_validate_json(row["manifest_json"])
        if (
            row["state"] != "published"
            or row["purpose"] != "action_review"
            or row["thread_id"] != str(thread_id)
            or row["turn_id"] != str(turn_id)
            or row["call_id"] != str(call_id)
            or row["workspace_scope"] != workspace_scope
            or row["artifact_id"] != str(artifact_id)
            or ref.artifact_id != artifact_id
            or ref.sha256 != hashlib.sha256(body).hexdigest()
            or ref.size_bytes != len(body)
            or ref.records != record_count
            or not ref.complete
            or row["body"] != body
        ):
            raise ValueError
        return ref
    except (ValueError, TypeError):
        raise KernelError("artifact_conflict", "Action Review身份已经绑定其他正文") from None


def validate_action_review_reference(
    row: aiosqlite.Row,
    turn: Turn,
    ref: ArtifactRef,
) -> ArtifactRef:
    """只有Session中的唯一Patch审批可使暂存Review对外可读。"""

    requests = [
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
        and str(item.content.call_id) == row["call_id"]
    ]
    if not requests:
        raise KernelError("artifact_unreferenced", "Action Review尚未绑定Session审批")
    if (
        len(requests) != 1
        or requests[0].presentation != "patch_batch"
        or requests[0].diff_artifact != ref
        or not ref.complete
    ):
        raise KernelError("artifact_corrupt", "Action Review引用不匹配")
    return ref
