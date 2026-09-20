"""Session共库容量、保留计划和恢复结果的低敏领域合同。"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
StoreKind = Literal["session", "protocol_request", "artifact"]
MaintenanceItemKind = Literal["artifact_body", "protocol_request", "session_thread"]
MaintenanceState = Literal["planned", "running", "completed"]


class _MaintenanceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoreCapacitySnapshot(_MaintenanceContract):
    """单类持久事实的计数水位；禁止携带业务身份或正文。"""

    spec_version: Literal["harnessix.store-capacity/v1"] = "harnessix.store-capacity/v1"
    store_kind: StoreKind
    logical_rows_by_kind: dict[str, int]
    oldest_created_at: AwareDatetime | None = None
    newest_created_at: AwareDatetime | None = None
    active_rows: int = Field(ge=0)
    terminal_rows: int = Field(ge=0)
    unknown_rows: int = Field(ge=0)
    artifact_body_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def bounded_counts(self) -> Self:
        if not self.logical_rows_by_kind or any(
            not key or type(value) is not int or value < 0
            for key, value in self.logical_rows_by_kind.items()
        ):
            raise ValueError("容量快照行计数不合法")
        if (
            self.oldest_created_at is not None
            and self.newest_created_at is not None
            and self.oldest_created_at > self.newest_created_at
        ):
            raise ValueError("容量快照时间范围倒置")
        return self


class StoreCapacityReport(_MaintenanceContract):
    """Session共库物理水位和三类逻辑容量的同一读事务快照。"""

    spec_version: Literal["harnessix.store-capacity-report/v1"] = (
        "harnessix.store-capacity-report/v1"
    )
    schema_version: int = Field(ge=1)
    database_bytes: int = Field(ge=0)
    wal_bytes: int = Field(ge=0)
    captured_at: AwareDatetime
    stores: tuple[StoreCapacitySnapshot, StoreCapacitySnapshot, StoreCapacitySnapshot]

    @model_validator(mode="after")
    def complete_store_set(self) -> Self:
        if tuple(item.store_kind for item in self.stores) != (
            "session",
            "protocol_request",
            "artifact",
        ):
            raise ValueError("容量报告必须按固定顺序包含三类Store")
        return self


class RetentionPolicy(_MaintenanceContract):
    """一次清理计划的显式Cutoff、容量和数据类别。"""

    spec_version: Literal["harnessix.retention-policy/v1"] = "harnessix.retention-policy/v1"
    cutoff: AwareDatetime
    max_items: int = Field(default=1000, ge=1, le=10000, strict=True)
    expire_artifact_bodies: bool = True
    delete_terminal_protocol_requests: bool = True
    delete_archived_threads: bool = True


class MaintenancePlan(_MaintenanceContract):
    """公开的不可变Dry Run结果；候选身份仅保存在内部Item表。"""

    spec_version: Literal["harnessix.store-maintenance-plan/v1"] = (
        "harnessix.store-maintenance-plan/v1"
    )
    plan_id: UUID
    policy: RetentionPolicy
    schema_version: int = Field(ge=1)
    created_at: AwareDatetime
    before: StoreCapacityReport
    candidates_by_kind: dict[MaintenanceItemKind, int]
    protected_by_reason: dict[str, int]
    candidate_set_sha256: Digest

    @model_validator(mode="after")
    def coherent_counts(self) -> Self:
        if any(type(value) is not int or value < 0 for value in self.candidates_by_kind.values()):
            raise ValueError("维护候选计数不合法")
        if any(
            not reason or type(value) is not int or value < 0
            for reason, value in self.protected_by_reason.items()
        ):
            raise ValueError("维护保护计数不合法")
        if sum(self.candidates_by_kind.values()) > self.policy.max_items:
            raise ValueError("维护候选超过计划上限")
        return self


class MaintenanceProgress(_MaintenanceContract):
    spec_version: Literal["harnessix.store-maintenance-progress/v1"] = (
        "harnessix.store-maintenance-progress/v1"
    )
    plan_id: UUID
    state: MaintenanceState
    total_items: int = Field(ge=0)
    next_ordinal: int = Field(ge=0)
    applied_items: int = Field(ge=0)
    skipped_items: int = Field(ge=0)
    backup_sha256: Digest | None = None
    started_at: AwareDatetime | None = None
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent_progress(self) -> Self:
        if (
            self.next_ordinal > self.total_items
            or self.applied_items + self.skipped_items != self.next_ordinal
            or (self.state == "planned" and (self.next_ordinal or self.backup_sha256 is not None))
            or (self.state == "running" and self.backup_sha256 is None)
            or (self.state == "completed" and self.next_ordinal != self.total_items)
            or (self.state == "completed") != (self.completed_at is not None)
        ):
            raise ValueError("维护进度不一致")
        return self


class MaintenanceExecutionReport(_MaintenanceContract):
    spec_version: Literal["harnessix.store-maintenance-result/v1"] = (
        "harnessix.store-maintenance-result/v1"
    )
    plan: MaintenancePlan
    progress: MaintenanceProgress
    after: StoreCapacityReport


class StoreRestoreReport(_MaintenanceContract):
    spec_version: Literal["harnessix.store-restore-result/v1"] = "harnessix.store-restore-result/v1"
    restored_at: AwareDatetime
    backup_sha256: Digest
    capacity: StoreCapacityReport
