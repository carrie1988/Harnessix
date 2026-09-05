from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import aiosqlite

from harnessix.agent.approvals import approval_for, request_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    ApprovalRequestContent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.agent.reducer import get_turn
from harnessix.artifacts.contracts import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_RECORDS,
    MAX_PAGE_BYTES,
    ArtifactPage,
    ArtifactPolicy,
    ArtifactRef,
    ArtifactToolResult,
    CollectionReport,
)
from harnessix.domain.models import ApprovalOutcome, EffectClass, utc_now
from harnessix.processes.output_artifact import parse_process_output_document
from harnessix.session.sqlite import SQLiteSessionStore


def _reject_constant(value: str) -> None:
    raise ValueError("JSON 不允许非有限数字")


def records(body: bytes) -> list[str]:
    if type(body) is not bytes or len(body) > MAX_ARTIFACT_BYTES:
        raise KernelError("artifact_invalid", "Artifact 正文类型或大小不符合契约")
    try:
        text = body.decode("utf-8")
        if text and not text.endswith("\n"):
            raise ValueError("记录未终止")
        lines = text.split("\n")[:-1] if text else []
        if len(lines) > MAX_ARTIFACT_RECORDS:
            raise ValueError("记录过多")
        for line in lines:
            if len(line.encode()) + 1 > MAX_PAGE_BYTES:
                raise ValueError("记录超过页上限")
            json.loads(line, parse_constant=_reject_constant)
        return lines
    except (ValueError, RecursionError):
        raise KernelError("artifact_invalid", "Artifact 不是受支持的有界 UTF-8 JSONL") from None


class SQLiteArtifactStore:
    """同一 Session 数据库内发布；正文不通过外部文件进行双写。"""

    def __init__(
        self,
        session: SQLiteSessionStore,
        *,
        policy: ArtifactPolicy | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or ArtifactPolicy()
        self._fault = fault or (lambda _: None)

    @property
    def session(self) -> SQLiteSessionStore:
        return self._session

    @property
    def policy(self) -> ArtifactPolicy:
        return self._policy

    def contract(self) -> dict[str, object]:
        return {
            "version": "sqlite-artifact/v1",
            "policy": self.policy.model_dump(mode="json"),
            "max_bytes": MAX_ARTIFACT_BYTES,
            "max_records": MAX_ARTIFACT_RECORDS,
            "page_bytes": MAX_PAGE_BYTES,
            "reference": ArtifactRef.model_json_schema(),
            "page": ArtifactPage.model_json_schema(),
        }

    async def publish(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        output: ArtifactToolResult,
        *,
        expected_sequence: int,
        max_output_chars: int,
    ) -> Thread:
        if output.publisher is not self:
            raise KernelError("artifact_store_mismatch", "Artifact 载荷绑定了不同的发布器")
        owner = self.session._runtime_owner_token
        if owner is None:
            raise KernelError("artifact_runtime_required", "Artifact 发布需要活跃的 Session 宿主")
        lines = records(output.body)
        result = ToolResultContent.model_validate_json(output.result.model_dump_json())
        if (
            result.outcome != "succeeded"
            or result.call_id != call.call_id
            or call.effect_class != EffectClass.READ_ONLY
            or not isinstance(output.workspace_scope, str)
            or not re.fullmatch(r"[0-9a-f]{64}", output.workspace_scope)
            or type(output.complete) is not bool
        ):
            raise KernelError("artifact_invalid", "Artifact 与只读调用结果不匹配")
        now = utc_now()
        ref = ArtifactRef(
            artifact_id=new_id(),
            sha256=hashlib.sha256(output.body).hexdigest(),
            size_bytes=len(output.body),
            records=len(lines),
            complete=output.complete,
            expires_at=now + timedelta(seconds=self.policy.ttl_seconds),
        )
        published = result.model_copy(
            update={"output": {"preview": result.output, "artifact": ref.model_dump(mode="json")}}
        )
        if len(published.model_dump_json()) > max_output_chars:
            raise KernelError("tool_output_too_large", "Artifact 引用与预览超过 Kernel 上限")
        item_id = new_id()
        batch = self.session._freeze_batch(
            (
                EventDraft(
                    turn_id=turn_id, payload=ItemStarted(item_id=item_id, content=published)
                ),
                EventDraft(
                    turn_id=turn_id,
                    payload=ItemFinished(
                        item_id=item_id, content=published, status=ItemStatus.COMPLETED
                    ),
                ),
            )
        )
        async with self.session._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            thread = await self.session._snapshot(database, thread_id)
            if thread is None or thread.sequence != expected_sequence:
                raise KernelError("sequence_conflict", "Artifact 发布时会话已变化")
            ToolExecutionScope.for_pending_call(thread, turn_id, call)
            turn = get_turn(thread, turn_id)
            if call.requires_approval:
                item = approval_for(turn, call)
                if (
                    item is None
                    or item.status != ItemStatus.COMPLETED
                    or not isinstance(item.content, ApprovalRequestContent)
                    or item.content.decision is None
                    or item.content.decision.outcome != ApprovalOutcome.APPROVED
                    or item.content.request_fingerprint != request_fingerprint(thread, turn, call)
                ):
                    raise KernelError("approval_mismatch", "Artifact 发布缺少匹配的批准")
            await self._check_quota(database, thread_id, turn_id, ref.size_bytes)
            await database.execute(
                "INSERT INTO agent_artifacts VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, 'tool_result')",
                (
                    str(ref.artifact_id),
                    str(thread_id),
                    str(turn_id),
                    str(call.call_id),
                    output.workspace_scope,
                    ref.model_dump_json(),
                    ref.size_bytes,
                    ref.expires_at.isoformat(),
                    output.body,
                ),
            )
            self._fault("artifact.after_insert")
            updated, _ = await self.session._append_in_transaction(
                database, thread_id, batch, expected_sequence=expected_sequence
            )
            self._fault("artifact.before_commit")
            if self.session._runtime_owner_token is not owner:
                raise KernelError("artifact_runtime_required", "Artifact 发布期间宿主已关闭")
            await database.commit()
            self._fault("artifact.after_commit")
            return updated

    async def _check_quota(
        self, database: aiosqlite.Connection, thread_id: UUID, turn_id: UUID, size: int
    ) -> None:
        cursor = await database.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(body)), 0), "
            "COALESCE(SUM(CASE WHEN thread_id = ? AND turn_id = ? THEN size_bytes ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN thread_id = ? AND turn_id = ? THEN 1 ELSE 0 END), 0) "
            "FROM agent_artifacts",
            (str(thread_id), str(turn_id), str(thread_id), str(turn_id)),
        )
        row = await cursor.fetchone()
        assert row is not None
        if (
            row[0] >= self.policy.max_manifests
            or row[1] + size > self.policy.max_live_bytes
            or row[2] + size > self.policy.max_turn_bytes
            or row[3] >= self.policy.max_turn_count
        ):
            raise KernelError("artifact_quota_exceeded", "Artifact 配额不足，未发布正文或结果")

    @staticmethod
    def _reference(row: aiosqlite.Row, thread: Thread) -> ArtifactRef:
        try:
            ref = ArtifactRef.model_validate_json(row["manifest_json"])
            if (
                str(ref.artifact_id) != row["artifact_id"]
                or ref.size_bytes != row["size_bytes"]
                or ref.expires_at.isoformat() != row["expires_at"]
            ):
                raise ValueError("索引不匹配")
            turn = get_turn(thread, UUID(row["turn_id"]))
            if row["purpose"] not in {
                "tool_result",
                "batch_plan",
                "batch_effect",
                "process_output",
            }:
                raise ValueError("未知归档用途")
            if row["purpose"] == "process_output":
                results = [
                    i.content
                    for i in turn.items
                    if isinstance(i.content, ToolResultContent)
                    and i.status == ItemStatus.COMPLETED
                    and str(i.content.call_id) == row["call_id"]
                    and i.content.process is not None
                ]
                requests = [
                    i.content
                    for i in turn.items
                    if isinstance(i.content, ProcessApprovalRequestContent)
                    and i.status == ItemStatus.COMPLETED
                    and str(i.content.call_id) == row["call_id"]
                ]
                if (
                    len(results) != 1
                    or len(requests) != 1
                    or not isinstance(results[0].output, dict)
                    or results[0].output.get("artifact") != ref.model_dump(mode="json")
                    or results[0].process is None
                    or results[0].action_id != requests[0].plan.action_id
                    or results[0].process.action_id != requests[0].plan.action_id
                    or results[0].process.action_fingerprint != requests[0].plan.action_fingerprint
                    or results[0].process.plan_fingerprint != requests[0].plan.approval_fingerprint
                ):
                    raise ValueError("Process输出引用不匹配")
                return ref
            if row["purpose"] != "tool_result":
                contents = [
                    i.content
                    for i in turn.items
                    if isinstance(i.content, PatchBatchApprovalRequestContent | ToolResultContent)
                    and (
                        isinstance(i.content, PatchBatchApprovalRequestContent)
                        if row["purpose"] == "batch_plan"
                        else isinstance(i.content, ToolResultContent)
                        and i.status == ItemStatus.COMPLETED
                        and i.content.patch_batch is not None
                    )
                    and str(i.content.call_id) == row["call_id"]
                ]
                if len(contents) != 1 or contents[0].diff_artifact != ref:
                    raise ValueError("差异引用不匹配")
                request = next(
                    i.content
                    for i in turn.items
                    if isinstance(i.content, PatchBatchApprovalRequestContent)
                    and str(i.content.call_id) == row["call_id"]
                )
                if request.plan.backend.manifest.workspace_scope != row["workspace_scope"]:
                    raise ValueError("差异工作区错绑")
                return ref
            results = [
                i.content
                for i in turn.items
                if isinstance(i.content, ToolResultContent)
                and str(i.content.call_id) == row["call_id"]
                and i.status == ItemStatus.COMPLETED
            ]
            if (
                len(results) != 1
                or results[0].outcome != "succeeded"
                or results[0].process is not None
                or not isinstance(results[0].output, dict)
                or results[0].output.get("artifact") != ref.model_dump(mode="json")
            ):
                raise ValueError("缺少结果引用")
            return ref
        except (ValueError, KernelError, StopIteration):
            raise KernelError("artifact_corrupt", "Artifact manifest 或结果引用不一致") from None

    async def read(
        self,
        thread_id: UUID,
        workspace_scope: str,
        artifact_id: UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ArtifactPage:
        if (
            type(offset) is not int
            or type(limit) is not int
            or not 0 <= offset <= MAX_ARTIFACT_RECORDS
            or not 1 <= limit <= 200
        ):
            raise KernelError("artifact_invalid_cursor", "Artifact 分页参数不合法")
        async with self.session._connection() as database:
            await database.execute("BEGIN")
            cursor = await database.execute(
                "SELECT * FROM agent_artifacts WHERE artifact_id = ? "
                "AND thread_id = ? AND workspace_scope = ?",
                (str(artifact_id), str(thread_id), workspace_scope),
            )
            row = await cursor.fetchone()
            if row is None:
                raise KernelError("artifact_not_found", "Artifact 不存在或不属于当前作用域")
            thread = await self.session._snapshot(database, thread_id)
            if thread is None:
                raise KernelError("artifact_corrupt", "Artifact 归属不存在")
            ref = self._reference(row, thread)
            if ref.expires_at <= utc_now() or row["state"] == "expired":
                raise KernelError("artifact_expired", "Artifact 已过期")
            body = row["body"]
            if (
                not isinstance(body, bytes)
                or len(body) != ref.size_bytes
                or hashlib.sha256(body).hexdigest() != ref.sha256
            ):
                raise KernelError("artifact_corrupt", "Artifact 正文校验失败")
            try:
                lines = records(body)
            except KernelError:
                raise KernelError("artifact_corrupt", "Artifact 记录损坏") from None
            if len(lines) != ref.records:
                raise KernelError("artifact_corrupt", "Artifact 记录数不一致")
            if row["purpose"] == "process_output":
                try:
                    document = parse_process_output_document(body)
                    turn = get_turn(thread, UUID(row["turn_id"]))
                    result = next(
                        i.content
                        for i in turn.items
                        if isinstance(i.content, ToolResultContent)
                        and i.status == ItemStatus.COMPLETED
                        and str(i.content.call_id) == row["call_id"]
                        and i.content.process is not None
                    )
                    assert isinstance(result.output, dict)
                    for name in ("stdout", "stderr"):
                        public = result.output[name]
                        stream = getattr(document.summary, name)
                        if not isinstance(public, dict) or public != {
                            "captured_bytes": stream.captured_bytes,
                            "observed_bytes": stream.observed_bytes,
                            "observed_sha256": stream.observed_sha256,
                            "truncated": stream.truncated,
                            "eof": stream.eof,
                        }:
                            raise ValueError("流摘要不匹配")
                    if document.summary.complete != ref.complete:
                        raise ValueError("完整性不匹配")
                except (AssertionError, KeyError, StopIteration, ValueError):
                    raise KernelError(
                        "artifact_corrupt", "Process Artifact正文与结果不一致"
                    ) from None
        if offset > len(lines):
            raise KernelError("artifact_invalid_cursor", "Artifact 偏移超过记录范围")
        selected, size = [], 0
        for line in lines[offset : offset + limit]:
            encoded = len(line.encode()) + 1
            if size + encoded > MAX_PAGE_BYTES:
                break
            selected.append(line + "\n")
            size += encoded
        end = offset + len(selected)
        return ArtifactPage(
            artifact=ref,
            offset=offset,
            text="".join(selected),
            next_offset=end if end < len(lines) else None,
        )

    async def collect(self, *, limit: int = 100, after: UUID | None = None) -> CollectionReport:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise KernelError("artifact_invalid_cursor", "清理批次大小不合法")
        now = utc_now()
        expired = protected = 0
        async with self.session._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            cursor = await database.execute(
                "SELECT * FROM agent_artifacts WHERE state = 'published' AND expires_at <= ? "
                "AND artifact_id > ? ORDER BY artifact_id LIMIT ?",
                (now.isoformat(), str(after) if after else "", limit),
            )
            rows = list(await cursor.fetchall())
            for row in rows:
                thread = await self.session._snapshot(database, UUID(row["thread_id"]))
                if thread is None:
                    raise KernelError("artifact_corrupt", "Artifact 归属不存在")
                self._reference(row, thread)
                if thread.active_turn_id is not None:
                    protected += 1
                    continue
                await database.execute(
                    "UPDATE agent_artifacts SET state = 'expired', body = NULL "
                    "WHERE artifact_id = ?",
                    (row["artifact_id"],),
                )
                expired += 1
            self._fault("artifact.before_collect_commit")
            await database.commit()
        return CollectionReport(
            len(rows),
            expired,
            protected,
            now,
            UUID(rows[-1]["artifact_id"]) if len(rows) == limit else None,
        )
