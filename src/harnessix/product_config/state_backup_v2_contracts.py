"""带Git业务剖面的完整产品备份v2合同；只定义闭合布局，不执行备份或恢复。"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.domain.models import ContractModel
from harnessix.product_config.state_backup_contracts import (
    DATABASES,
    KEY_FILE,
    MAX_STATE_BYTES,
    MAX_STATE_FILE_BYTES,
    MAX_STATE_FILES,
    PROCESS_DATABASE,
    Digest,
    ephemeral_state_file,
    state_directory_allowed,
    state_file_kind,
)

GIT_DATABASE = "git-delivery/git-delivery.db"
GIT_BUSINESS_SNAPSHOT = "git-delivery/business-snapshot.json"
V2_DATABASES = (*DATABASES, GIT_DATABASE)
StateFileKindV2 = Literal["database", "key", "blob", "process", "business_snapshot"]
FileIdentity = tuple[
    Annotated[int, Field(ge=0)],
    Annotated[int, Field(gt=0)],
]


def state_file_kind_v2(path: str) -> StateFileKindV2:
    """v2只在v1闭合集合上增加固定GitDB和固定只读业务快照。"""
    if path == GIT_DATABASE:
        return "database"
    if path == GIT_BUSINESS_SNAPSHOT:
        return "business_snapshot"
    return state_file_kind(path)


def ephemeral_state_file_v2(path: str) -> bool:
    """GitDB的SQLite生命周期文件由Backup API消费，不作为恢复成员复制。"""
    return ephemeral_state_file(path) or path in {
        GIT_DATABASE + suffix for suffix in ("-wal", "-shm", "-journal")
    }


def state_directory_allowed_v2(path: str) -> bool:
    """允许扫描固定Git目录本身；其worktree、anchor和临时子树仍然拒绝。"""
    return path == "git-delivery" or state_directory_allowed(path)


class ProductStateGitBackupProfile(ContractModel):
    """Manifest对同一GitDB v2尾锚和业务快照的不可变引用。"""

    profile: Literal["git-delivery"] = "git-delivery"
    git_store_schema_version: Literal["2"] = "2"
    capture_id: UUID
    store_genesis_epoch: UUID
    prefix_revision: int = Field(ge=0, le=2**63 - 1)
    # 名称沿已冻结合同；值是尾锚认证的规范 GitPrefixCatalog 原字节摘要。
    prefix_anchor_sha256: Digest
    business_snapshot_sha256: Digest


class ProductStateFileV2(ContractModel):
    path: str = Field(min_length=1, max_length=160)
    kind: StateFileKindV2
    size_bytes: int = Field(ge=0, le=MAX_STATE_FILE_BYTES)
    sha256: Digest

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        if state_file_kind_v2(self.path) != self.kind:
            raise ValueError("备份路径与类别不匹配")
        return self


class ProductStateBackupManifestV2(ContractModel):
    """启用Git业务状态后的备份清单；v1无Git布局保持原合同不变。"""

    spec_version: Literal["harnessix.product-state-backup/v2"] = "harnessix.product-state-backup/v2"
    backup_id: UUID
    created_at: AwareDatetime
    platform: Literal["posix", "nt"]
    store_id: UUID
    key_id: UUID
    captured_state_root_identity: FileIdentity
    business_profiles: tuple[ProductStateGitBackupProfile] = Field(min_length=1, max_length=1)
    # 先让领域校验器报告缺少哪一类固定成员，而不是由通用长度错误掩盖根因。
    files: tuple[ProductStateFileV2, ...] = Field(min_length=7, max_length=MAX_STATE_FILES)

    @model_validator(mode="after")
    def complete_layout(self) -> Self:
        paths = [entry.path for entry in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("备份清单重复或顺序无效")
        required = {*V2_DATABASES, KEY_FILE, GIT_BUSINESS_SNAPSHOT}
        if not required <= set(paths):
            raise ValueError("Git业务剖面缺少固定受管成员")
        if sum(entry.size_bytes for entry in self.files) > MAX_STATE_BYTES:
            raise ValueError("备份总容量超限")
        if any(entry.kind == "process" for entry in self.files) and PROCESS_DATABASE not in paths:
            raise ValueError("Process事实缺少原Lease库")
        snapshot = next(entry for entry in self.files if entry.path == GIT_BUSINESS_SNAPSHOT)
        profile = self.business_profiles[0]
        if profile.capture_id != self.backup_id:
            raise ValueError("Git业务快照捕获身份与备份不匹配")
        if snapshot.sha256 != profile.business_snapshot_sha256:
            raise ValueError("Git业务快照摘要与剖面不匹配")
        return self


class ProductStateBackupReceiptV2(ContractModel):
    """根外可信回执；以原字节摘要绑定v2清单和逻辑Store／Key。"""

    spec_version: Literal["harnessix.product-state-backup-receipt/v2"] = (
        "harnessix.product-state-backup-receipt/v2"
    )
    backup_id: UUID
    manifest_sha256: Digest
    store_id: UUID
    key_id: UUID
