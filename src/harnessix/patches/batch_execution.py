"""宿主整组一次性消费；复用单文件引擎，不承诺跨文件原子提交。"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from uuid import UUID

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import batch_runs, ledger
from harnessix.patches.batch_run_contracts import BatchExecutionResult, BatchRunRecord, StopReason
from harnessix.patches.batches import verify_patch_batch
from harnessix.patches.managed_io import fail, writable_target
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import ReadOperation

if TYPE_CHECKING:
    from harnessix.patches.managed_batches import ManagedPatchBatches


def _fault(point: str) -> None:
    """组消费、成员调度和终态发布的真实退出切点。"""


def inspect(
    groups: ManagedPatchBatches, batch_id: UUID, operation: ReadOperation
) -> BatchExecutionResult | None:
    approval, _ = groups._load(batch_id, operation)
    run = batch_runs.load(groups.copy._db, approval)
    if run is None:
        return None
    records = tuple(groups.copy._load(m.plan_id, operation)[0] for m in approval.plan.members)
    operation.checkpoint()
    return batch_runs.result(run, records)


def _finish(
    groups: ManagedPatchBatches, run: BatchRunRecord, reason: StopReason, code: str | None
) -> BatchExecutionResult:
    # 取消后只结算已有事实；新的有界读预算不用于后续文件调度。
    operation = ReadOperation()
    groups.copy._validate()
    current = inspect(groups, run.batch_id, operation)
    if current is None or current.run != run or run.phase != "started":
        raise fail("ledger_corrupt")
    finished = BatchRunRecord.model_validate_json(
        run.model_copy(
            update={"phase": "finished", "stop_reason": reason, "error_code": code}
        ).model_dump_json()
    )
    result = BatchExecutionResult(run=finished, members=current.members, effect=current.effect)
    with ledger.transaction(groups.copy._db):
        batch_runs.append(groups.copy._db, finished)
        _fault("run_result_before_commit")
    _fault("run_result_committed")
    return result


def _reason(code: str | None) -> StopReason:
    if code == "cancelled":
        return "cancelled"
    return "timeout" if code in {"timeout", "patch_timeout"} else "failed"


def execute(
    groups: ManagedPatchBatches,
    batch_id: UUID,
    fingerprint: str,
    operation: ReadOperation,
) -> BatchExecutionResult:
    copy = groups.copy
    with copy._guard():
        approval, batch = groups._load(batch_id, operation)
        if approval.plan.approval_fingerprint != fingerprint:
            raise fail("approval_mismatch")
        if (
            approval.decision is None
            or approval.decision.outcome.value != "approved"
            or batch_runs.load(copy._db, approval) is not None
        ):
            raise fail("not_executable")
        run = BatchRunRecord(
            batch_id=batch_id,
            workspace_id=copy.workspace_id,
            approval_fingerprint=fingerprint,
            phase="started",
        )
        with ledger.transaction(copy._db):
            operation.checkpoint()
            batch_runs.append(copy._db, run)
            _fault("run_before_commit")
            operation.checkpoint()
        reason: StopReason = "completed"
        code = None
        unexpected: BaseException | None = None
        try:
            _fault("run_started")
            verify_patch_batch(copy.workspace, batch, operation)
            for patch in batch.patches:
                writable_target(copy.workspace, patch.manifest.path, operation)
            _fault("preflight_complete")
            for index, member in enumerate(approval.plan.members):
                operation.checkpoint()
                copy._validate()
                # 每成员准入验证整组身份、运行阶段及成功前缀，不能凭成员指纹单独写。
                current = inspect(groups, batch_id, operation)
                if (
                    current is None
                    or current.run != run
                    or any(m.state != "applied" for m in current.members[:index])
                    or any(m.state != "pending" for m in current.members[index:])
                ):
                    raise fail("ledger_corrupt")
                record, _, _ = copy._load(member.plan_id, operation)
                copy._append(record, "approved", None, decision=approval.decision)
                _fault(f"member_approved:{index}")
                result = copy._execute(member.plan_id, member.approval_fingerprint, operation)
                _fault(f"member_completed:{index}")
                operation.checkpoint()
                if result.state != "applied":
                    code = result.error_code
                    reason = _reason(code)
                    break
        except BaseException as error:
            code = (
                error.code
                if isinstance(error, (KernelError, ReadToolError))
                else "cancelled"
                if isinstance(error, TurnCancelled)
                else "patch_execution_failed"
            )
            reason = _reason(code)
            if not isinstance(
                error, (KernelError, ReadToolError, OSError, sqlite3.Error, TurnCancelled)
            ):
                unexpected = error
        outcome = _finish(groups, run, reason, code)
        if unexpected is not None:
            raise unexpected
        return outcome


def reconcile(
    groups: ManagedPatchBatches, batch_id: UUID, operation: ReadOperation
) -> BatchExecutionResult | None:
    with groups.copy._guard():
        current = inspect(groups, batch_id, operation)
        if current is None:
            return None
        for index, member in enumerate(current.members):
            operation.checkpoint()
            if member.state in {"started", "uncertain"}:
                groups.copy._reconcile(member.plan_id, operation)
                _fault(f"member_reconciled:{index}")
        operation.checkpoint()
        if current.run.phase == "started":
            return _finish(groups, current.run, "interrupted", None)
        return inspect(groups, batch_id, operation)
