"""Workspace事务Action共享执行器：单租约逐成员发布与只观察恢复。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import build_workspace_action_review
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
    ) -> None:
        if type(lease_seconds) not in {int, float} or not 0 < lease_seconds <= 3600:
            raise ValueError("Workspace Patch租约时长无效")
        self._transactions = transactions
        self._record_loader = record_loader
        self._runtime = WorkspaceTransactionRuntime(transactions, leases)
        self._leases = leases
        self._workspace_root = workspace_root
        self._lease_seconds = float(lease_seconds)

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
                output={
                    "transaction_id": str(record.transaction_id),
                    "files": len(record.plan.mutations),
                    "state": record.state,
                    "origin": origin,
                    "diff_sha256": diff.sha256,
                },
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
