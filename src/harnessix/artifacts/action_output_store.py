"""Trusted Process Action输出的查询优先发布与Session引用校验。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import aiosqlite

from harnessix.agent.approvals import approval_for, approval_matches
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.agent.reducer import get_turn
from harnessix.artifacts.contracts import ArtifactPolicy, ArtifactRef
from harnessix.artifacts.persistence import insert_artifact
from harnessix.domain.models import ApprovalOutcome, EffectClass, utc_now
from harnessix.processes.trusted_output import (
    parse_trusted_process_output,
    trusted_process_public_output,
)
from harnessix.session.sqlite import SQLiteSessionStore

QuotaCheck = Callable[[aiosqlite.Connection, UUID, UUID, int], Awaitable[None]]
FaultInjector = Callable[[str], None]


class ActionOutputArtifactMixin:
    """把Trusted Process输出发布职责从通用SQLite Store中隔离。"""

    _fault: FaultInjector

    @property
    def session(self) -> SQLiteSessionStore:
        raise NotImplementedError

    @property
    def policy(self) -> ArtifactPolicy:
        raise NotImplementedError

    async def action_recovery_inventory(self) -> tuple[tuple[str, UUID], ...]:
        """读取Action专用Artifact的Purpose与Call身份；不返回正文、路径或摘要。"""

        async with self.session._connection() as database:
            cursor = await database.execute(
                "SELECT purpose, call_id FROM agent_artifacts "
                "WHERE purpose IN ('action_review', 'action_output') "
                "ORDER BY purpose, call_id"
            )
            rows = await cursor.fetchall()
        try:
            return tuple((row[0], UUID(row[1])) for row in rows)
        except (TypeError, ValueError):
            raise KernelError("artifact_corrupt", "Action Artifact索引损坏") from None

    async def _check_quota(
        self,
        database: aiosqlite.Connection,
        thread_id: UUID,
        turn_id: UUID,
        size: int,
    ) -> None:
        raise NotImplementedError

    async def publish_action_output(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        body: bytes,
        *,
        artifact_id: UUID,
        workspace_scope: str,
        expected_sequence: int,
    ) -> ArtifactRef:
        """查询优先发布可信Process终态输出；结果提交前正文保持不可读。"""

        from harnessix.artifacts.sqlite import records

        return await publish_action_output(
            self.session,
            self.policy,
            self._fault,
            self._check_quota,
            thread_id,
            turn_id,
            call,
            body,
            artifact_id=artifact_id,
            workspace_scope=workspace_scope,
            expected_sequence=expected_sequence,
            record_count=len(records(body)),
        )


def _validated_output_reference(
    policy: ArtifactPolicy,
    call: ToolCallContent,
    body: bytes,
    *,
    artifact_id: UUID,
    workspace_scope: str,
    record_count: int,
) -> tuple[ArtifactRef, str]:
    """校验可信输出正文、写调用和Workspace，并生成待发布收据。"""

    try:
        document = parse_trusted_process_output(body)
    except ValueError:
        raise KernelError("artifact_invalid", "Action输出正文不符合可信Process契约") from None
    if (
        not call.requires_approval
        or call.effect_class is EffectClass.READ_ONLY
        or not re.fullmatch(r"[0-9a-f]{64}", workspace_scope)
    ):
        raise KernelError("artifact_invalid", "Action输出与写调用或Workspace不匹配")
    return (
        ArtifactRef(
            artifact_id=artifact_id,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            records=record_count,
            complete=document.summary.complete,
            expires_at=utc_now() + timedelta(seconds=policy.ttl_seconds),
        ),
        document.summary.process_id,
    )


def _require_approved_process_output(
    thread: Thread,
    turn_id: UUID,
    call: ToolCallContent,
    process_id: str,
) -> None:
    """要求输出绑定同一Session中已批准且未漂移的Process计划。"""

    turn = get_turn(thread, turn_id)
    item = approval_for(turn, call)
    if (
        item is None
        or item.status is not ItemStatus.COMPLETED
        or not isinstance(item.content, TrustedActionApprovalRequestContent)
        or item.content.presentation != "process"
        or item.content.decision is None
        or item.content.decision.outcome is not ApprovalOutcome.APPROVED
        or not approval_matches(thread, turn, call, item.content)
        or str(item.content.plan_id) != process_id
    ):
        raise KernelError("approval_mismatch", "Action输出缺少匹配的Process批准")


async def publish_action_output(
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
    """发布或查询同一终态输出收据；Session结果提交前正文不可见。"""

    owner = session._runtime_owner_token
    if owner is None:
        raise KernelError("artifact_runtime_required", "Action输出发布需要活跃Session宿主")
    candidate, process_id = _validated_output_reference(
        policy,
        call,
        body,
        artifact_id=artifact_id,
        workspace_scope=workspace_scope,
        record_count=record_count,
    )
    published_at = candidate.expires_at - timedelta(seconds=policy.ttl_seconds)
    try:
        async with session._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            existing = await _stored_output(database, call.call_id)
            if existing is not None:
                matched = matching_action_output(
                    existing,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    call_id=call.call_id,
                    artifact_id=artifact_id,
                    workspace_scope=workspace_scope,
                    body=body,
                    record_count=record_count,
                    complete=candidate.complete,
                )
                await database.commit()
                return matched
            thread = await session._snapshot(database, thread_id)
            if thread is None or thread.sequence != expected_sequence:
                raise KernelError("sequence_conflict", "Action输出发布时会话已变化")
            ToolExecutionScope.for_pending_call(thread, turn_id, call)
            _require_approved_process_output(thread, turn_id, call, process_id)
            await check_quota(database, thread_id, turn_id, candidate.size_bytes)
            await insert_artifact(
                database,
                candidate,
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call.call_id,
                workspace_scope=workspace_scope,
                body=body,
                purpose="action_output",
                created_at=published_at,
            )
            fault("action_output.after_insert")
            if session._runtime_owner_token is not owner:
                raise KernelError("artifact_runtime_required", "Action输出发布期间宿主已关闭")
            await database.commit()
            fault("action_output.after_commit")
            return candidate
    except Exception:
        # COMMIT确认丢失时只匹配原身份；不生成新Artifact或刷新TTL。
        async with session._connection() as database:
            existing = await _stored_output(database, call.call_id)
            if existing is not None:
                return matching_action_output(
                    existing,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    call_id=call.call_id,
                    artifact_id=artifact_id,
                    workspace_scope=workspace_scope,
                    body=body,
                    record_count=record_count,
                    complete=candidate.complete,
                )
        raise


async def _stored_output(
    database: aiosqlite.Connection,
    call_id: UUID,
) -> aiosqlite.Row | None:
    cursor = await database.execute(
        "SELECT * FROM agent_artifacts WHERE call_id = ? AND purpose = 'action_output'",
        (str(call_id),),
    )
    return await cursor.fetchone()


def matching_action_output(
    row: aiosqlite.Row,
    *,
    thread_id: UUID,
    turn_id: UUID,
    call_id: UUID,
    artifact_id: UUID,
    workspace_scope: str,
    body: bytes,
    record_count: int,
    complete: bool,
) -> ArtifactRef:
    """精确匹配稳定身份、作用域、正文、完整性和原始TTL。"""

    try:
        ref = ArtifactRef.model_validate_json(row["manifest_json"])
        if (
            row["state"] != "published"
            or row["purpose"] != "action_output"
            or row["thread_id"] != str(thread_id)
            or row["turn_id"] != str(turn_id)
            or row["call_id"] != str(call_id)
            or row["workspace_scope"] != workspace_scope
            or row["artifact_id"] != str(artifact_id)
            or ref.artifact_id != artifact_id
            or ref.sha256 != hashlib.sha256(body).hexdigest()
            or ref.size_bytes != len(body)
            or ref.records != record_count
            or ref.complete != complete
            or row["body"] != body
        ):
            raise ValueError
        return ref
    except (ValueError, TypeError):
        raise KernelError("artifact_conflict", "Action输出身份已经绑定其他正文") from None


def validate_action_output_body(
    row: aiosqlite.Row,
    thread: Thread,
    ref: ArtifactRef,
    body: bytes,
) -> None:
    """核对归档正文摘要与已提交Session Tool Result完全一致。"""

    try:
        document = parse_trusted_process_output(body)
        turn = get_turn(thread, UUID(row["turn_id"]))
        result = _single_result(turn, row["call_id"])
        if not isinstance(result.output, dict):
            raise ValueError
        public = {key: value for key, value in result.output.items() if key != "artifact"}
        expected = trusted_process_public_output(
            document,
            include_passed="passed" in public,
        )
        if (
            public != expected
            or result.output.get("artifact") != ref.model_dump(mode="json")
            or document.summary.complete != ref.complete
        ):
            raise ValueError
    except (StopIteration, ValueError):
        raise KernelError("artifact_corrupt", "Trusted Action Artifact正文与结果不一致") from None


def _single_result(turn: Turn, call_id: str) -> ToolResultContent:
    results = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
        and item.status is ItemStatus.COMPLETED
        and str(item.content.call_id) == call_id
        and item.content.trusted_action is not None
    ]
    if len(results) != 1:
        raise ValueError
    return results[0]


def _single_request(turn: Turn, call_id: str) -> TrustedActionApprovalRequestContent:
    requests = [
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
        and item.status is ItemStatus.COMPLETED
        and str(item.content.call_id) == call_id
    ]
    if len(requests) != 1:
        raise ValueError
    return requests[0]


def validate_action_output_reference(
    row: aiosqlite.Row,
    thread: Thread,
    turn: Turn,
    ref: ArtifactRef,
) -> ArtifactRef:
    """仅同一已批准Process Route的唯一终态结果可授权输出正文。"""

    try:
        result = _single_result(turn, row["call_id"])
    except ValueError:
        raise KernelError("artifact_unreferenced", "Action输出尚未绑定Session终态结果") from None
    try:
        request = _single_request(turn, row["call_id"])
    except ValueError:
        raise KernelError("artifact_corrupt", "Action输出结果或审批数量不匹配") from None
    effect = result.trusted_action
    if (
        request.presentation != "process"
        or request.decision is None
        or request.decision.outcome is not ApprovalOutcome.APPROVED
        or not approval_matches(thread, turn, _call(turn, row["call_id"]), request)
        or effect is None
        or effect.plan_id != request.plan_id
        or effect.plan_fingerprint != request.plan_fingerprint
        or effect.artifact_sha256 != ref.sha256
        or not isinstance(result.output, dict)
        or result.output.get("artifact") != ref.model_dump(mode="json")
    ):
        raise KernelError("artifact_corrupt", "Action输出引用与批准Route不匹配")
    return ref


def _call(turn: Turn, call_id: str) -> ToolCallContent:
    matches = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolCallContent) and str(item.content.call_id) == call_id
    ]
    if len(matches) != 1:
        raise KernelError("artifact_corrupt", "Action输出缺少唯一Tool Call")
    return matches[0]
