"""完整产品备份的闭合布局与有限清单；摘要证明复制一致，信任来自根外本机回执。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.domain.models import ContractModel

MAX_STATE_FILES = 4096
MAX_STATE_BYTES = 2 * 1024**3
MAX_STATE_FILE_BYTES = 256 * 1024**2
MAX_BACKUP_MANIFEST_BYTES = 1024**2
MAX_STATE_ROWS = 100_000
MAX_STATE_PAYLOAD_BYTES = 64 * 1024**2

DATABASES = (
    "action-audit.db",
    "execution-plans.db",
    "product-config.db",
    "sessions.db",
    "workspace-leases.db",
    "workspace-transactions/transactions.db",
)
PROCESS_DATABASE = "process-owner/process-leases.db"
KEY_FILE = "session-auth/key.v1"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]


def state_file_kind(path: str) -> Literal["database", "key", "blob", "process"]:
    """路径属于受管事实的闭合集合；不接受绝对路径、别名、任意扩展或执行程序。"""
    if str(PurePosixPath(path)) != path or path.startswith("/") or "\\" in path:
        raise ValueError("备份路径不规范")
    if path in (*DATABASES, PROCESS_DATABASE):
        return "database"
    if path == KEY_FILE:
        return "key"
    if re.fullmatch(r"workspace-transactions/blobs/[0-9a-f]{64}", path):
        return "blob"
    match = re.fullmatch(
        r"process-owner/runs/([0-9a-f-]{36})/(receipt\.json|stdout\.bin|stderr\.bin)", path
    )
    if match and str(UUID(match[1])) == match[1]:
        return "process"
    raise ValueError("备份包含未受管事实")


def ephemeral_state_file(path: str) -> bool:
    """WAL由SQLite Backup消费；OS锁与初始化锁不是可恢复的业务所有权。"""
    return path in {
        "session-auth/.lock",
        "product-action-runtime.lock",
        "sessions.db.runtime.lock",
        "action-audit.db.runtime.lock",
    } or any(
        path == database + suffix
        for database in (*DATABASES, PROCESS_DATABASE)
        for suffix in ("-wal", "-shm", "-journal")
    )


def state_directory_allowed(path: str) -> bool:
    if path in {
        "session-auth",
        "workspace-transactions",
        "workspace-transactions/blobs",
        "process-owner",
        "process-owner/runs",
    }:
        return True
    match = re.fullmatch(r"process-owner/runs/([0-9a-f-]{36})", path)
    return bool(match and str(UUID(match[1])) == match[1])


class ProductStateFile(ContractModel):
    path: str = Field(min_length=1, max_length=160)
    kind: Literal["database", "key", "blob", "process"]
    size_bytes: int = Field(ge=0, le=MAX_STATE_FILE_BYTES)
    sha256: Digest

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        if state_file_kind(self.path) != self.kind:
            raise ValueError("备份路径与类别不匹配")
        return self


class ProductStateBackupManifest(ContractModel):
    spec_version: Literal["harnessix.product-state-backup/v1"] = "harnessix.product-state-backup/v1"
    backup_id: UUID
    created_at: AwareDatetime
    platform: Literal["posix", "nt"]
    store_id: UUID
    key_id: UUID
    files: tuple[ProductStateFile, ...] = Field(min_length=7, max_length=MAX_STATE_FILES)

    @model_validator(mode="after")
    def complete_layout(self) -> Self:
        paths = [entry.path for entry in self.files]
        if paths != sorted(set(paths)) or not set((*DATABASES, KEY_FILE)) <= set(paths):
            raise ValueError("备份清单缺失、重复或顺序无效")
        if sum(entry.size_bytes for entry in self.files) > MAX_STATE_BYTES:
            raise ValueError("备份总容量超限")
        if any(entry.kind == "process" for entry in self.files) and PROCESS_DATABASE not in paths:
            raise ValueError("Process事实缺少原Lease库")
        return self


class ProductStateBackupReceipt(ContractModel):
    """只能从原Root的外部私有锚点读取，不能由不可信备份自身提供。"""

    spec_version: Literal["harnessix.product-state-backup-receipt/v1"] = (
        "harnessix.product-state-backup-receipt/v1"
    )
    backup_id: UUID
    manifest_sha256: Digest
    store_id: UUID
    key_id: UUID
