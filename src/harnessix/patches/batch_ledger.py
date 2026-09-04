"""整组持久数据的交叉校验；所有写事务由宿主入口持有。"""

import json
import sqlite3
from uuid import UUID, uuid4

from harnessix.domain.models import ApprovalDecision
from harnessix.patches import ledger
from harnessix.patches.batch_approval_contracts import (
    MAX_BATCH_DECISION_BYTES,
    MAX_BATCH_METADATA_BYTES,
    MAX_BATCH_PLAN_BYTES,
    ManagedPatchBatchApproval,
    ManagedPatchBatchPlan,
    PatchBatchMember,
    member_request_id,
)
from harnessix.patches.batch_contracts import PatchBatchProposal, PreparedPatchBatch
from harnessix.patches.batches import validate_patch_batch
from harnessix.patches.managed_contracts import PatchRecord
from harnessix.patches.managed_io import fail
from harnessix.tools.workspace import ReadOperation, Workspace, digest


def plan(
    workspace_id: UUID, request_id: str, batch: PreparedPatchBatch
) -> tuple[ManagedPatchBatchPlan, tuple[PatchRecord, ...]]:
    batch_id = uuid4()
    records = []
    for position, patch in enumerate(batch.patches):
        record = PatchRecord(
            plan_id=uuid4(),
            workspace_id=workspace_id,
            request_id=member_request_id(
                workspace_id, batch_id, request_id, position, patch.manifest.fingerprint
            ),
            manifest=patch.manifest,
            approval_fingerprint="0" * 64,
            state="pending",
        )
        records.append(
            record.model_copy(update={"approval_fingerprint": ledger.fingerprint(record)})
        )
    data = {
        "version": "managed-patch-batch-plan/v1",
        "workspace_id": str(workspace_id),
        "batch_id": str(batch_id),
        "request_id": request_id,
        "manifest": batch.manifest.model_dump(mode="json"),
        "members": [
            PatchBatchMember(
                plan_id=record.plan_id,
                request_id=record.request_id,
                approval_fingerprint=record.approval_fingerprint,
            ).model_dump(mode="json")
            for record in records
        ],
    }
    return (
        ManagedPatchBatchPlan.model_validate_json(
            json.dumps({**data, "approval_fingerprint": digest(data)})
        ),
        tuple(records),
    )


def capacity(db: sqlite3.Connection, payload: str) -> None:
    size = db.execute(
        "SELECT coalesce(sum(length(CAST(payload AS BLOB)) + ?),0) FROM batches",
        (MAX_BATCH_DECISION_BYTES,),
    ).fetchone()[0]
    if size + len(payload.encode()) + MAX_BATCH_DECISION_BYTES > MAX_BATCH_METADATA_BYTES:
        raise fail("batch_metadata_limit_exceeded")


def require_single(db: sqlite3.Connection, plan_id: UUID) -> None:
    row = db.execute("SELECT owner_batch_id FROM plans WHERE id=?", (str(plan_id),)).fetchone()
    if row is None:
        raise fail("plan_not_found")
    if row[0] is not None:
        raise fail("batch_member_requires_group")
    # 同时检查完整组计划，不能仅将被清空的归属列当作单文件授权。
    rows = db.execute(
        "SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END, checksum "
        "FROM batches LIMIT 65",
        (MAX_BATCH_PLAN_BYTES,),
    ).fetchall()
    try:
        if len(rows) > 64:
            raise ValueError
        for payload, checksum in rows:
            if payload is None or digest(payload) != checksum:
                raise ValueError
            stored = ManagedPatchBatchPlan.model_validate_json(payload)
            if any(member.plan_id == plan_id for member in stored.members):
                raise ValueError
    except (ValueError, TypeError):
        raise fail("ledger_corrupt") from None


def load(
    db: sqlite3.Connection,
    workspace: Workspace,
    workspace_id: UUID,
    batch_id: UUID,
    operation: ReadOperation,
) -> tuple[ManagedPatchBatchApproval, PreparedPatchBatch]:
    operation.checkpoint()
    row = db.execute(
        "SELECT request_id, CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END, "
        "checksum FROM batches WHERE id=?",
        (MAX_BATCH_PLAN_BYTES, str(batch_id)),
    ).fetchone()
    if row is None:
        raise fail("batch_not_found")
    try:
        if row[1] is None or row[2] != digest(row[1]):
            raise ValueError
        stored = ManagedPatchBatchPlan.model_validate_json(row[1])
        if (
            stored.batch_id != batch_id
            or stored.workspace_id != workspace_id
            or stored.request_id != row[0]
            or stored.manifest.workspace_scope != workspace.scope
        ):
            raise ValueError
        owned = db.execute(
            "SELECT id FROM plans WHERE owner_batch_id=? LIMIT 17", (str(batch_id),)
        ).fetchall()
        if {row[0] for row in owned} != {str(member.plan_id) for member in stored.members}:
            raise ValueError
        patches = []
        for member, manifest in zip(stored.members, stored.manifest.files, strict=True):
            record, prepared, _ = ledger.load(
                db, workspace, workspace_id, member.plan_id, operation
            )
            if (
                record.request_id != member.request_id
                or record.approval_fingerprint != member.approval_fingerprint
                or record.manifest != manifest
                or record.state != "pending"
                or record.decision is not None
            ):
                raise ValueError
            patches.append(prepared)
        batch = PreparedPatchBatch(
            stored.manifest,
            PatchBatchProposal(files=tuple(p.proposal for p in patches)),
            tuple(patches),
        )
        validate_patch_batch(workspace, batch, operation)
        row = db.execute(
            "SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END, checksum "
            "FROM batch_approvals WHERE batch_id=?",
            (MAX_BATCH_DECISION_BYTES, str(batch_id)),
        ).fetchone()
        decision = None
        if row is not None:
            if row[0] is None or row[1] != digest(
                (str(batch_id), stored.approval_fingerprint, row[0])
            ):
                raise ValueError
            decision = ApprovalDecision.model_validate_json(row[0])
        operation.checkpoint()
        return ManagedPatchBatchApproval(plan=stored, decision=decision), batch
    except (ValueError, TypeError):
        raise fail("ledger_corrupt") from None
