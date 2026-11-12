"""Workspace事务Action共享执行器：单租约逐成员发布与只观察恢复。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, JsonValue

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceDiffDocument, WorkspaceTransactionRecord
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import build_workspace_action_review
from harnessix.execution.public_tool_contracts import (
    MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES,
    WORKSPACE_PATCH_DIFF_PREVIEW_MARKER,
)
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRoutePlan
from harnessix.workspace.leases import WorkspaceLeaseStore


class WorkspaceTransactionActionExecutor:
    """以Workspace Lease逐成员发布；取消后由Router转UNKNOWN且不自动续写。"""

    def __init__(
        self,
        transactions: SQLiteWorkspaceTransactionStore,
        leases: WorkspaceLeaseStore,
        workspace_root: Callable[[str], Path],
        record_loader: Callable[[ActionRoutePlan, BaseModel], WorkspaceTransactionRecord],
        *,
        lease_seconds: float = 300.0,
        after_publish: Callable[[ActionRoutePlan, BaseModel, Path], None] | None = None,
    ) -> None:
        if type(lease_seconds) not in {int, float} or not 0 < lease_seconds <= 3600:
            raise ValueError("Workspace Patch租约时长无效")
        self._transactions = transactions
        self._record_loader = record_loader
        self._runtime = WorkspaceTransactionRuntime(transactions, leases)
        self._leases = leases
        self._workspace_root = workspace_root
        self._lease_seconds = float(lease_seconds)
        self._after_publish = after_publish

    async def execute(
        self,
        plan: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        record = self._record_loader(plan, arguments)
        root = self._workspace_root(plan.execution.workspace.workspace_id)
        lease = self._leases.acquire(
            record.plan.source.workspace_id,
            f"workspace-patch:{plan.execution.plan_id}",
            ttl_seconds=self._lease_seconds,
        )
        try:
            while record.state != "published":
                # Task取消只在尚未开始下一个原子成员时生效；已进入的单成员必须完成记账。
                await asyncio.sleep(0)
                record = self._runtime.publish_next(
                    record.transaction_id,
                    root,
                    approval_fingerprint=record.plan.fingerprint,
                    lease=lease,
                )
            if self._after_publish is not None:
                # 仍持有Workspace租约：发布后的收尾不会与另一笔事务的成员写入交错。
                self._after_publish(plan, arguments, root)
        finally:
            self._leases.release(lease)
        return self._outcome(record, origin="execution")

    async def reconcile(
        self,
        plan: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        try:
            record = self._record_loader(plan, arguments)
        except KernelError as error:
            if error.code == "delivery_transaction_not_found":
                return ActionExecutionOutcome(
                    kind="manual_intervention",
                    error_code="delivery_plan_missing",
                )
            raise
        root = self._workspace_root(plan.execution.workspace.workspace_id)
        record = self._runtime.reconcile(record.transaction_id, root)
        return self._outcome(record, origin="recovery")

    def _outcome(
        self,
        record: WorkspaceTransactionRecord,
        *,
        origin: str,
    ) -> ActionExecutionOutcome:
        if record.state == "published":
            diff = build_workspace_diff(record.plan, self._transactions)
            review = build_workspace_action_review(record.plan, diff.entries, diff.text)
            return ActionExecutionOutcome(
                kind="succeeded",
                output=_published_output(record, diff, origin),
                artifact_sha256=hashlib.sha256(review.to_jsonl()).hexdigest(),
            )
        if record.state == "prepared" or (record.state == "interrupted" and record.cursor == 0):
            return ActionExecutionOutcome(kind="failed", error_code="delivery_not_applied")
        if origin == "execution":
            return ActionExecutionOutcome(kind="unknown", error_code="delivery_effect_unknown")
        return ActionExecutionOutcome(
            kind="manual_intervention",
            error_code=(
                "delivery_partial_effect"
                if record.state == "interrupted"
                else record.error_code or "delivery_effect_unknown"
            ),
        )


def _published_output(
    record: WorkspaceTransactionRecord, diff: WorkspaceDiffDocument, origin: str
) -> dict[str, JsonValue]:
    """v2成功摘要：各文件发布后摘要与有界Diff预览，供下一次编辑直接作expected_sha256。

    published只在每个成员的磁盘观察（完整字节SHA、大小、权限）等于计划after版本后才会记录，
    因此after.sha256就是已发布字节的摘要，而不是前镜像或模型提交的正文。
    """
    return {
        "spec_version": "harnessix.workspace-patch-output/v2",
        "transaction_id": str(record.transaction_id),
        "files": len(record.plan.mutations),
        "state": record.state,
        "origin": origin,
        "diff_sha256": diff.sha256,
        "changes": [
            {"path": mutation.path, "content_sha256": mutation.after.sha256}
            for mutation in record.plan.mutations
        ],
        "diff_preview": _diff_preview(diff.text, diff.utf8_bytes),
    }


def _diff_preview(text: str, total_bytes: int) -> dict[str, JsonValue]:
    """完整Diff的有界前缀：尽量在行尾截断，未截断时即完整Diff本身。"""
    if total_bytes <= MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES:
        return {"text": text, "total_bytes": total_bytes, "truncated": False}
    marker = WORKSPACE_PATCH_DIFF_PREVIEW_MARKER
    limit = MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES - len(marker.encode())
    # 每个字符至少1字节：先按字符切片，避免为4 KiB前缀编码最多64 MiB的完整Diff。
    head = text[:limit].encode("utf-8")[:limit]
    line_end = head.rfind(b"\n")
    # 正文是严格UTF-8；无换行时ignore只丢弃边界上未完整的最后一个码点。
    shown = head[: line_end + 1].decode() if line_end >= 0 else head.decode(errors="ignore")
    return {"text": shown + marker, "total_bytes": total_bytes, "truncated": True}
