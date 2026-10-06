"""独立物理 stored-record/v3，仅承载事务领域 v2，不扩张旧壳 Schema。"""

from __future__ import annotations

from typing import Literal

from harnessix.delivery.workspace_record_contracts import WorkspaceStoredRecord


class WorkspaceStoredRecordV3(WorkspaceStoredRecord):
    """复用原有界 Plan 引用和全部状态字段，领域代际严格绑定 record/v2。"""

    spec_version: Literal["harnessix.workspace-stored-record/v3"] = (
        "harnessix.workspace-stored-record/v3"  # type: ignore[assignment]
    )
    domain_spec_version: Literal["harnessix.workspace-transaction-record/v2"] = (
        "harnessix.workspace-transaction-record/v2"  # type: ignore[assignment]
    )
