"""完整状态恢复的闭合意图与结果；原Manifest字节和目录身份不由恢复时临时重建。"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.domain.models import ContractModel
from harnessix.product_config.state_backup_contracts import (
    MAX_BACKUP_MANIFEST_BYTES,
    Digest,
    ProductStateBackupManifest,
)

ACTIVE_RESTORE_FILE = "restore-active.json"
MAX_RESTORE_RECORD_BYTES = 3 * MAX_BACKUP_MANIFEST_BYTES
FileIdentity = tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(gt=0)]]
RestoreMode = Literal["complete", "rollback"]


class ProductStateRestorePlan(ContractModel):
    """持久原目录、候选及父目录身份，以及原回执已经授权的Manifest字节。"""

    spec_version: Literal["harnessix.product-state-restore-plan/v1"] = (
        "harnessix.product-state-restore-plan/v1"
    )
    restore_id: UUID
    owner_address_key: str = Field(pattern=r"^\.harnessix-state-owner-[0-9a-f]{64}$")
    parent_identity: FileIdentity
    previous_identity: FileIdentity | None
    candidate_identity: FileIdentity
    manifest_json: str = Field(min_length=1, max_length=MAX_BACKUP_MANIFEST_BYTES)

    @model_validator(mode="after")
    def valid_manifest(self) -> Self:
        if len(self.manifest_json.encode()) > MAX_BACKUP_MANIFEST_BYTES:
            raise ValueError("恢复Manifest字节超限")
        ProductStateBackupManifest.model_validate_json(self.manifest_json)
        if self.previous_identity == self.candidate_identity:
            raise ValueError("恢复候选与原状态身份冲突")
        return self

    @property
    def manifest(self) -> ProductStateBackupManifest:
        """返回原可信Manifest的正式合同，不补签或替换摘要。"""
        return ProductStateBackupManifest.model_validate_json(self.manifest_json)


class ProductStateRestorePointer(ContractModel):
    """活动指针以计划原字节Hash阻止半写或错配日志驱动Root切换。"""

    spec_version: Literal["harnessix.product-state-restore-pointer/v1"] = (
        "harnessix.product-state-restore-pointer/v1"
    )
    restore_id: UUID
    plan_sha256: Digest


class ProductStateRestoreResult(ContractModel):
    """耐久终态记录；重复请求只读此结果，不声称当前Root仍等于旧快照。"""

    spec_version: Literal["harnessix.product-state-restore-result/v1"] = (
        "harnessix.product-state-restore-result/v1"
    )
    restore_id: UUID
    backup_id: UUID
    status: Literal["restored", "rolled_back"]
    retained_previous_state: bool
    completed_at: AwareDatetime


class ProductStateRollbackDecision(ContractModel):
    """回退决定一旦发布不可改为继续恢复，跨崩溃沿用同一计划。"""

    restore_id: UUID
    plan_sha256: Digest
    mode: Literal["rollback"] = "rollback"


def journal_directory(restore_id: UUID) -> str:
    """仅允许从原请求UUID派生的私有目录，不接受日志中的任意路径。"""
    return f"restore-{restore_id}"
