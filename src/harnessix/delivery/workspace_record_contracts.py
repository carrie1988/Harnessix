"""Workspace记录的物理引用合同；与原完整领域记录及其摘要分别版本化。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    MAX_TRANSACTION_FILES,
    DeliveryContract,
    TransactionState,
)
from harnessix.tools.contracts import Revision

MAX_STORED_RECORD_BYTES = 512 * 1024


class WorkspacePlanReference(DeliveryContract):
    """同时绑定完整Plan原字节与领域指纹；摘要地址不授予执行权限。"""

    sha256: Revision
    size: int = Field(gt=0, le=MAX_TRANSACTION_FILE_BYTES)
    fingerprint: Revision


class WorkspaceStoredRecord(DeliveryContract):
    """物理v2壳保存全部状态字段；解引用后必须再验证原领域Record。"""

    spec_version: Literal["harnessix.workspace-stored-record/v2"] = (
        "harnessix.workspace-stored-record/v2"
    )
    domain_spec_version: Literal["harnessix.workspace-transaction-record/v1"] = (
        "harnessix.workspace-transaction-record/v1"
    )
    transaction_id: UUID
    plan_ref: WorkspacePlanReference
    state: TransactionState
    sequence: int = Field(ge=0, le=4096)
    cursor: int = Field(ge=0, le=MAX_TRANSACTION_FILES)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    record_digest: Revision
