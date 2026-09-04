"""借用受管副本锁的宿主整组预留/审批；当前不提供整组执行。"""

from uuid import UUID

from harnessix.domain.models import ApprovalDecision
from harnessix.patches import batch_ledger, ledger
from harnessix.patches.batch_approval_contracts import ManagedPatchBatchApproval
from harnessix.patches.batch_contracts import PreparedPatchBatch
from harnessix.patches.batches import validate_patch_batch, verify_patch_batch
from harnessix.patches.managed import ManagedPatchWorkspace
from harnessix.patches.managed_io import fail
from harnessix.tools.workspace import ReadOperation, digest


def _fault(point: str) -> None:
    """整组事务真实退出与存储故障注入切点。"""


def _request(request_id: str) -> None:
    if type(request_id) is not str or not 1 <= len(request_id) <= 128:
        raise fail("invalid_request")


class ManagedPatchBatches:
    def __init__(self, copy: ManagedPatchWorkspace) -> None:
        self.copy = copy

    def _load(
        self, batch_id: UUID, operation: ReadOperation
    ) -> tuple[ManagedPatchBatchApproval, PreparedPatchBatch]:
        if type(batch_id) is not UUID:
            raise fail("invalid_batch")
        result = batch_ledger.load(
            self.copy._db, self.copy.workspace, self.copy.workspace_id, batch_id, operation
        )
        paths = {entry.path for entry in self.copy.manifest.files}
        if any(manifest.path not in paths for manifest in result[0].plan.manifest.files):
            raise fail("path_denied")
        return result

    def _lookup_id(self, request_id: str) -> UUID | None:
        _request(request_id)
        row = self.copy._db.execute(
            "SELECT id FROM batches WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return UUID(row[0])
        except (ValueError, TypeError, AttributeError):
            raise fail("ledger_corrupt") from None

    def save(
        self, batch: PreparedPatchBatch, request_id: str, operation: ReadOperation
    ) -> ManagedPatchBatchApproval:
        with self.copy._guard():
            _request(request_id)
            validate_patch_batch(self.copy.workspace, batch, operation)
            batch_id = self._lookup_id(request_id)
            if batch_id is not None:
                existing, prepared = self._load(batch_id, operation)
                if prepared != batch:
                    raise fail("request_conflict")
                return existing
            paths = {entry.path for entry in self.copy.manifest.files}
            if any(p.manifest.path not in paths for p in batch.patches):
                raise fail("path_denied")
            verify_patch_batch(self.copy.workspace, batch, operation)
            plan, members = batch_ledger.plan(self.copy.workspace_id, request_id, batch)
            payload = plan.model_dump_json()
            with ledger.transaction(self.copy._db):
                ledger.capacity(
                    self.copy._db,
                    len(members),
                    sum(len(p.before) + len(p.after) for p in batch.patches),
                )
                batch_ledger.capacity(self.copy._db, payload)
                self.copy._db.execute(
                    "INSERT INTO batches VALUES(?,?,?,?)",
                    (str(plan.batch_id), request_id, payload, digest(payload)),
                )
                _fault("batch_reserved")
                for index, (member, patch) in enumerate(zip(members, batch.patches, strict=True)):
                    operation.checkpoint()
                    ledger.insert(self.copy._db, member, patch, plan.batch_id)
                    _fault(f"member_reserved:{index}")
                operation.checkpoint()
                _fault("reservation_before_commit")
                operation.checkpoint()
            _fault("reservation_committed")
            return ManagedPatchBatchApproval(plan=plan)

    def get(self, batch_id: UUID, operation: ReadOperation) -> ManagedPatchBatchApproval:
        with self.copy._guard():
            return self._load(batch_id, operation)[0]

    def lookup(self, request_id: str, operation: ReadOperation) -> ManagedPatchBatchApproval | None:
        with self.copy._guard():
            operation.checkpoint()
            batch_id = self._lookup_id(request_id)
            return None if batch_id is None else self._load(batch_id, operation)[0]

    def verify(self, batch_id: UUID, operation: ReadOperation) -> ManagedPatchBatchApproval:
        with self.copy._guard():
            approval, batch = self._load(batch_id, operation)
            verify_patch_batch(self.copy.workspace, batch, operation)
            return approval

    def reply(
        self,
        batch_id: UUID,
        approval_fingerprint: str,
        decision: ApprovalDecision,
        operation: ReadOperation,
    ) -> ManagedPatchBatchApproval:
        with self.copy._guard():
            approval, _ = self._load(batch_id, operation)
            if approval.plan.approval_fingerprint != approval_fingerprint:
                raise fail("approval_mismatch")
            decision = ApprovalDecision.model_validate_json(decision.model_dump_json())
            result = ManagedPatchBatchApproval(plan=approval.plan, decision=decision)
            if approval.decision is not None:
                if approval.decision != decision:
                    raise fail("approval_conflict")
                return approval
            payload = decision.model_dump_json()
            with ledger.transaction(self.copy._db):
                operation.checkpoint()
                self.copy._db.execute(
                    "INSERT INTO batch_approvals VALUES(?,?,?)",
                    (
                        str(batch_id),
                        payload,
                        digest((str(batch_id), approval_fingerprint, payload)),
                    ),
                )
                _fault("approval_before_commit")
                operation.checkpoint()
            _fault("approval_committed")
            return result
