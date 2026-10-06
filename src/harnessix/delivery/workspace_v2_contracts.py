"""独立 Workspace 事务 v2；复用原完整校验、指纹与状态字段。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import TypeAdapter

from harnessix.delivery.contracts import (
    TransactionState,
    WorkspaceTransactionPlan,
    WorkspaceTransactionRecord,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2


class WorkspaceTransactionPlanV2(WorkspaceTransactionPlan):
    """新指纹绑定实际 Snapshot v2 和完整父引用，旧 Plan Schema 不扩张。"""

    spec_version: Literal["harnessix.workspace-transaction-plan/v2"] = (
        "harnessix.workspace-transaction-plan/v2"  # type: ignore[assignment]
    )
    source: WorkspaceSnapshotV2  # type: ignore[assignment]


class WorkspaceTransactionRecordV2(WorkspaceTransactionRecord):
    """全部状态不变量沿用原 Record；序列化始终采用新实际领域类型。"""

    spec_version: Literal["harnessix.workspace-transaction-record/v2"] = (
        "harnessix.workspace-transaction-record/v2"  # type: ignore[assignment]
    )
    plan: WorkspaceTransactionPlanV2


def _validated_record(payload: dict[str, object]) -> WorkspaceTransactionRecordV2:
    # 字典先计算真实 JSON 摘要，再执行完整模型校验；不构造未校验中间 Record。
    wire = TypeAdapter(dict[str, object]).dump_python(payload, mode="json", warnings="error")
    payload["record_digest"] = canonical_digest(wire)
    return WorkspaceTransactionRecordV2.model_validate(payload, strict=True)


def new_transaction_record_v2(plan: WorkspaceTransactionPlanV2) -> WorkspaceTransactionRecordV2:
    """创建完整校验的 prepared 记录，不使用旧类型序列化新父引用。"""
    return _validated_record(
        dict(
            spec_version="harnessix.workspace-transaction-record/v2",
            transaction_id=plan.transaction_id,
            plan=plan,
            state="prepared",
            sequence=0,
            cursor=0,
            started_at=None,
            finished_at=None,
            error_code=None,
        )
    )


def transition_transaction_record_v2(
    record: WorkspaceTransactionRecordV2,
    *,
    state: TransactionState,
    cursor: int,
    now: datetime,
    error_code: str | None = None,
) -> WorkspaceTransactionRecordV2:
    """沿用原状态字段计算；原 Store 状态机仍负责迁移和并发准入。"""
    return _validated_record(
        dict(
            spec_version="harnessix.workspace-transaction-record/v2",
            transaction_id=record.transaction_id,
            plan=record.plan,
            state=state,
            sequence=record.sequence + 1,
            cursor=cursor,
            started_at=record.started_at or now,
            finished_at=now if state in {"published", "diverged", "unknown"} else None,
            error_code=error_code,
        )
    )
