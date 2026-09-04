"""宿主整组计划与决定；批准不等于执行许可已被消费。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from harnessix.domain.models import ApprovalDecision
from harnessix.patches.batch_contracts import MAX_BATCH_FILES, PatchBatchManifest
from harnessix.tools.contracts import ReadContract, Revision
from harnessix.tools.workspace import digest

MAX_BATCH_PLAN_BYTES = 64 * 1024
MAX_BATCH_DECISION_BYTES = 16 * 1024
MAX_BATCH_METADATA_BYTES = 1024 * 1024


def member_request_id(
    workspace_id: UUID, batch_id: UUID, request_id: str, position: int, fingerprint: str
) -> str:
    return digest(
        (
            "managed-patch-batch-member/v1",
            str(workspace_id),
            str(batch_id),
            request_id,
            position,
            fingerprint,
        )
    )


class PatchBatchMember(ReadContract):
    plan_id: UUID
    request_id: Revision
    approval_fingerprint: Revision


class ManagedPatchBatchPlan(ReadContract):
    version: Literal["managed-patch-batch-plan/v1"] = "managed-patch-batch-plan/v1"
    batch_id: UUID
    workspace_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    manifest: PatchBatchManifest
    members: tuple[PatchBatchMember, ...] = Field(min_length=1, max_length=MAX_BATCH_FILES)
    approval_fingerprint: Revision

    @model_validator(mode="after")
    def bound_members(self) -> Self:
        if len(self.members) != len(self.manifest.files) or len(
            {member.plan_id for member in self.members}
        ) != len(self.members):
            raise ValueError("组成员数量不匹配或身份重复")
        for position, (member, manifest) in enumerate(
            zip(self.members, self.manifest.files, strict=True)
        ):
            if member.request_id != member_request_id(
                self.workspace_id, self.batch_id, self.request_id, position, manifest.fingerprint
            ) or member.approval_fingerprint != digest(
                (
                    str(self.workspace_id),
                    str(member.plan_id),
                    member.request_id,
                    manifest.fingerprint,
                )
            ):
                raise ValueError("组成员绑定不一致")
        if self.approval_fingerprint != digest(
            self.model_dump(mode="json", exclude={"approval_fingerprint"})
        ):
            raise ValueError("整组审批指纹不一致")
        if len(self.model_dump_json().encode()) > MAX_BATCH_PLAN_BYTES:
            raise ValueError("整组持久计划超过字节上限")
        return self


class ManagedPatchBatchApproval(ReadContract):
    version: Literal["managed-patch-batch-approval/v1"] = "managed-patch-batch-approval/v1"
    plan: ManagedPatchBatchPlan
    decision: ApprovalDecision | None = None

    @model_validator(mode="after")
    def bounded_decision(self) -> Self:
        if self.decision is not None and (
            len(self.decision.model_dump_json().encode()) > MAX_BATCH_DECISION_BYTES
        ):
            raise ValueError("整组审批决定超过字节上限")
        return self
