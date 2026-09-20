"""可信Action恢复：定义Runtime栅栏、Route操作租约与低敏扫描合同。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.domain.models import ContractModel
from harnessix.execution.contracts import canonical_digest
from harnessix.tools.contracts import Revision

ActionOperationPhase = Literal["execute", "reconcile"]
ActionOperationState = Literal["active", "completed", "interrupted"]


class ActionRuntimeFence(ContractModel):
    """一次产品Action Runtime所有权；原始Token只保存在当前宿主内存。"""

    spec_version: Literal["harnessix.action-runtime-fence/v1"] = "harnessix.action-runtime-fence/v1"
    generation: int = Field(ge=1)
    token: Revision = Field(repr=False)
    acquired_at: AwareDatetime


class ActionRouteOperation(ContractModel):
    """一次Execute/Reconcile的持久期限与Owner绑定。"""

    spec_version: Literal["harnessix.action-route-operation/v1"] = (
        "harnessix.action-route-operation/v1"
    )
    operation_id: UUID
    plan_id: UUID
    phase: ActionOperationPhase
    attempt: int = Field(ge=1, le=128)
    owner_generation: int = Field(ge=1)
    owner_token_sha256: Revision
    started_at: AwareDatetime
    deadline: AwareDatetime
    state: ActionOperationState = "active"
    completion_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def complete_operation(self) -> Self:
        if self.deadline <= self.started_at:
            raise ValueError("Action Route操作截止时间无效")
        terminal = self.state != "active"
        if terminal != (self.completed_at is not None):
            raise ValueError("Action Route操作终态时间不完整")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("Action Route操作终态时间缺少时区")
        if self.state == "interrupted" and self.completion_code is None:
            raise ValueError("中断Action Route操作缺少原因")
        return self


class ClaimedActionOperation(ContractModel):
    """当前宿主持有的操作能力；Token不会进入持久正文、日志或公开报告。"""

    operation: ActionRouteOperation
    token: Revision = Field(repr=False)


class ActionRecoveryScanReport(ContractModel):
    """启动恢复前后的低敏完整性汇总，不包含Plan ID、路径或错误正文。"""

    spec_version: Literal["harnessix.action-recovery-scan/v1"] = "harnessix.action-recovery-scan/v1"
    owner_generation: int = Field(ge=1)
    scanned_routes: int = Field(ge=0)
    repaired_execution_plans: int = Field(ge=0)
    invalid_execution_plans: int = Field(ge=0)
    active_operations: int = Field(ge=0)
    expired_operations: int = Field(ge=0)
    process_orphan_leases: int = Field(ge=0)
    session_orphan_references: int = Field(ge=0)
    routes_without_session_reference: int = Field(ge=0)
    artifact_orphans: int = Field(ge=0)
    created_at: AwareDatetime
    report_sha256: Revision

    @model_validator(mode="after")
    def valid_report(self) -> Self:
        if (
            self.repaired_execution_plans + self.invalid_execution_plans > self.scanned_routes
            or self.expired_operations > self.active_operations
            or self.report_sha256 != action_recovery_scan_report_digest(self)
        ):
            raise ValueError("Action恢复扫描报告不一致")
        return self


def action_recovery_scan_report_digest(report: ActionRecoveryScanReport) -> str:
    return canonical_digest(
        report.model_dump(mode="json", exclude={"report_sha256"}, warnings="error")
    )
