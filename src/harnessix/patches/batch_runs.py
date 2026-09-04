"""组运行事件及成员顺序的交叉校验；不负责文件修改。"""

import sqlite3

from harnessix.patches.batch_approval_contracts import ManagedPatchBatchApproval
from harnessix.patches.batch_run_contracts import (
    APPLIED,
    MAX_RUN_EVENT_BYTES,
    BatchExecutionResult,
    BatchMemberEffect,
    BatchRunRecord,
    aggregate,
    member_effect,
    ordered_progress,
)
from harnessix.patches.managed_contracts import PatchRecord
from harnessix.patches.managed_io import fail
from harnessix.tools.workspace import digest


def load(db: sqlite3.Connection, approval: ManagedPatchBatchApproval) -> BatchRunRecord | None:
    # 仅用于启动时校验真实 v2 旧组；v2 不可能具有合法执行事实。
    if db.execute("PRAGMA user_version").fetchone()[0] == 2:
        return None
    rows = db.execute(
        "SELECT phase, CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END, checksum "
        "FROM batch_run_events WHERE batch_id=? ORDER BY sequence LIMIT 3",
        (MAX_RUN_EVENT_BYTES, str(approval.plan.batch_id)),
    ).fetchall()
    try:
        if len(rows) > 2:
            raise ValueError
        result = None
        for index, (phase, payload, checksum) in enumerate(rows):
            if payload is None or checksum != digest(payload):
                raise ValueError
            result = BatchRunRecord.model_validate_json(payload)
            if (
                result.phase != phase
                or phase != ("started" if index == 0 else "finished")
                or result.batch_id != approval.plan.batch_id
                or result.workspace_id != approval.plan.workspace_id
                or result.approval_fingerprint != approval.plan.approval_fingerprint
                or approval.decision is None
                or approval.decision.outcome.value != "approved"
            ):
                raise ValueError
        return result
    except (ValueError, TypeError):
        raise fail("ledger_corrupt") from None


def append(db: sqlite3.Connection, record: BatchRunRecord) -> None:
    record = BatchRunRecord.model_validate_json(record.model_dump_json())
    payload = record.model_dump_json()
    db.execute(
        "INSERT INTO batch_run_events(batch_id,phase,payload,checksum) VALUES(?,?,?,?)",
        (str(record.batch_id), record.phase, payload, digest(payload)),
    )


def validate_members(
    approval: ManagedPatchBatchApproval,
    run: BatchRunRecord | None,
    records: tuple[PatchRecord, ...],
) -> None:
    try:
        if run is None:
            if any(record.state != "pending" or record.decision is not None for record in records):
                raise ValueError
            return
        ordered_progress(tuple(record.state for record in records))
        for record in records:
            if record.state != "pending" and record.decision != approval.decision:
                raise ValueError
        if run.stop_reason == "completed" and any(
            record.state not in APPLIED for record in records
        ):
            raise ValueError
    except ValueError:
        raise fail("ledger_corrupt") from None


def result(run: BatchRunRecord, records: tuple[PatchRecord, ...]) -> BatchExecutionResult:
    members = tuple(
        BatchMemberEffect(
            plan_id=record.plan_id,
            state=record.state,
            effect=member_effect(record.state),
            error_code=record.error_code,
        )
        for record in records
    )
    return BatchExecutionResult(run=run, members=members, effect=aggregate(members))
