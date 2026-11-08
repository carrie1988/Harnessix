"""保留现有文件权限的Workspace事务代际（ADR 0068第2版）。

原合同用Git能表示的两种权限（0644/0755）记录文件版本，但磁盘上的真实权限还包括0664、0777等，
因此修改这类已有文件时只能拒绝。本代际记录任意普通权限位（0～0777，特殊位仍拒绝），
只在一笔事务涉及0644/0755以外的权限时使用；其余事务仍生成原代际，字节与摘要不变。
两条Snapshot路线各有一个新Plan版本：v3承接原Snapshot（v1），v4承接Snapshot v2（v2）。
Git交付不接受本代际的事务（见product_config/git_delivery_source.py）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import Field, TypeAdapter

from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILES,
    TransactionState,
    WorkspaceDiffDocument,
    WorkspaceDiffEntry,
    WorkspaceFileVersion,
    WorkspaceMutation,
    WorkspaceTransactionPlan,
    WorkspaceTransactionRecord,
)
from harnessix.delivery.workspace_record_contracts import WorkspaceStoredRecord
from harnessix.delivery.workspace_v2_contracts import (
    WorkspaceTransactionPlanV2,
    WorkspaceTransactionRecordV2,
)
from harnessix.execution.contracts import canonical_digest

# 普通权限位；setuid、setgid、sticky位在观察文件时拒绝，不进入合同。
PermissionMode = Annotated[int, Field(ge=0, le=0o777)]
GIT_REPRESENTABLE_MODES = frozenset({0o644, 0o755})


class WorkspaceFileVersionV2(WorkspaceFileVersion):
    """文件版本的完整性规则沿用原合同，只把权限放宽为真实的普通权限位。"""

    mode: PermissionMode | None = None  # type: ignore[assignment]


class WorkspaceMutationV2(WorkspaceMutation):
    version_type: ClassVar[type[WorkspaceFileVersion]] = WorkspaceFileVersionV2
    before: WorkspaceFileVersionV2
    after: WorkspaceFileVersionV2


class WorkspaceTransactionPlanV3(WorkspaceTransactionPlan):
    """原Snapshot来源的保留权限计划；路径、容量与指纹规则沿用v1。"""

    mutation_type: ClassVar[type[WorkspaceMutation]] = WorkspaceMutationV2
    spec_version: Literal["harnessix.workspace-transaction-plan/v3"] = (
        "harnessix.workspace-transaction-plan/v3"  # type: ignore[assignment]
    )
    mutations: tuple[WorkspaceMutationV2, ...] = Field(
        min_length=1, max_length=MAX_TRANSACTION_FILES
    )


class WorkspaceTransactionPlanV4(WorkspaceTransactionPlanV2):
    """Snapshot v2来源的保留权限计划；父引用与完整父历史规则沿用v2。"""

    mutation_type: ClassVar[type[WorkspaceMutation]] = WorkspaceMutationV2
    spec_version: Literal["harnessix.workspace-transaction-plan/v4"] = (
        "harnessix.workspace-transaction-plan/v4"  # type: ignore[assignment]
    )
    mutations: tuple[WorkspaceMutationV2, ...] = Field(
        min_length=1, max_length=MAX_TRANSACTION_FILES
    )


class WorkspaceTransactionRecordV3(WorkspaceTransactionRecord):
    spec_version: Literal["harnessix.workspace-transaction-record/v3"] = (
        "harnessix.workspace-transaction-record/v3"  # type: ignore[assignment]
    )
    plan: WorkspaceTransactionPlanV3


class WorkspaceTransactionRecordV4(WorkspaceTransactionRecordV2):
    spec_version: Literal["harnessix.workspace-transaction-record/v4"] = (
        "harnessix.workspace-transaction-record/v4"  # type: ignore[assignment]
    )
    plan: WorkspaceTransactionPlanV4


class WorkspaceStoredRecordV4(WorkspaceStoredRecord):
    """保留权限代际的物理壳；旧程序不认识该版本，读取时失败关闭。"""

    spec_version: Literal["harnessix.workspace-stored-record/v4"] = (
        "harnessix.workspace-stored-record/v4"  # type: ignore[assignment]
    )
    domain_spec_version: Literal[  # type: ignore[assignment]
        "harnessix.workspace-transaction-record/v3",
        "harnessix.workspace-transaction-record/v4",
    ]


class WorkspaceDiffEntryV2(WorkspaceDiffEntry):
    before: WorkspaceFileVersionV2
    after: WorkspaceFileVersionV2


class WorkspaceDiffDocumentV2(WorkspaceDiffDocument):
    spec_version: Literal["harnessix.workspace-diff/v2"] = "harnessix.workspace-diff/v2"  # type: ignore[assignment]
    entries: tuple[WorkspaceDiffEntryV2, ...] = Field(
        min_length=1, max_length=MAX_TRANSACTION_FILES
    )


PreservedModePlan = WorkspaceTransactionPlanV3 | WorkspaceTransactionPlanV4
PreservedModeRecord = WorkspaceTransactionRecordV3 | WorkspaceTransactionRecordV4


def preserves_file_modes(plan: WorkspaceTransactionPlan) -> bool:
    """该计划是否属于保留权限代际（可能含0644/0755以外的权限）。"""
    return isinstance(plan, WorkspaceTransactionPlanV3 | WorkspaceTransactionPlanV4)


def needs_preserved_modes(modes: list[int | None]) -> bool:
    """事务涉及的所有文件权限都能用原合同表示时返回False，原代际字节保持不变。"""
    return any(mode is not None and mode not in GIT_REPRESENTABLE_MODES for mode in modes)


def _record_type(plan: WorkspaceTransactionPlan) -> type[PreservedModeRecord]:
    if type(plan) is WorkspaceTransactionPlanV3:
        return WorkspaceTransactionRecordV3
    if type(plan) is WorkspaceTransactionPlanV4:
        return WorkspaceTransactionRecordV4
    raise ValueError("不是保留权限代际的Workspace计划")


def _validated_record(
    record_type: type[PreservedModeRecord], payload: dict[str, object]
) -> PreservedModeRecord:
    # 与v2相同：字典先计算真实JSON摘要，再执行完整模型校验；不构造未校验中间记录。
    payload["spec_version"] = record_type.model_fields["spec_version"].default
    wire = TypeAdapter(dict[str, object]).dump_python(payload, mode="json", warnings="error")
    payload["record_digest"] = canonical_digest(wire)
    return record_type.model_validate(payload, strict=True)


def new_preserved_mode_record(plan: PreservedModePlan) -> PreservedModeRecord:
    return _validated_record(
        _record_type(plan),
        dict(
            transaction_id=plan.transaction_id,
            plan=plan,
            state="prepared",
            sequence=0,
            cursor=0,
            started_at=None,
            finished_at=None,
            error_code=None,
        ),
    )


def transition_preserved_mode_record(
    record: PreservedModeRecord,
    *,
    state: TransactionState,
    cursor: int,
    now: datetime,
    error_code: str | None = None,
) -> PreservedModeRecord:
    """状态字段的计算与原记录相同；原Store状态机仍负责迁移与并发准入。"""
    return _validated_record(
        _record_type(record.plan),
        dict(
            transaction_id=record.transaction_id,
            plan=record.plan,
            state=state,
            sequence=record.sequence + 1,
            cursor=cursor,
            started_at=record.started_at or now,
            finished_at=now if state in {"published", "diverged", "unknown"} else None,
            error_code=error_code,
        ),
    )
