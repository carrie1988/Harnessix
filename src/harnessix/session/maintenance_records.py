"""维护计划内部记录：候选摘要、不可变计划与可恢复进度。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import aiosqlite
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.session.capacity import ThreadFact
from harnessix.session.maintenance_contracts import (
    MaintenanceItemKind,
    MaintenancePlan,
    MaintenanceProgress,
)


@dataclass(frozen=True, slots=True)
class PlanItem:
    kind: MaintenanceItemKind
    key: dict[str, str]
    precondition_sha256: str

    @property
    def key_json(self) -> str:
        return canonical(self.key)

    @property
    def key_sha256(self) -> str:
        return digest(self.key_json)


def canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError):
        raise KernelError("maintenance_corrupt", "维护记录不能规范化") from None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def row_precondition(row: aiosqlite.Row, fields: tuple[str, ...]) -> str:
    return digest(canonical({field: row[field] for field in fields}))


def artifact_precondition(row: aiosqlite.Row) -> str:
    return row_precondition(
        row,
        (
            "artifact_id",
            "thread_id",
            "manifest_json",
            "size_bytes",
            "expires_at",
            "state",
            "purpose",
            "created_at",
        ),
    )


def request_precondition(row: aiosqlite.Row) -> str:
    return row_precondition(
        row,
        (
            "client_instance_id",
            "request_id",
            "method",
            "params_sha256",
            "state",
            "outcome_sha256",
            "created_at",
            "updated_at",
        ),
    )


def thread_precondition(fact: ThreadFact) -> str:
    return digest(
        canonical(
            {
                "thread_id": str(fact.thread.thread_id),
                "sequence": fact.thread.sequence,
                "snapshot_sha256": fact.snapshot_sha256,
            }
        )
    )


def parse_time(value: object, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise KernelError(code, "维护候选时间字段损坏")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise KernelError(code, "维护候选时间字段损坏") from None
    if parsed.tzinfo is None:
        raise KernelError(code, "维护候选时间缺少时区")
    return parsed


def candidate_set_sha256(items: list[PlanItem]) -> str:
    return digest(
        canonical(
            [
                {
                    "kind": item.kind,
                    "key_sha256": item.key_sha256,
                    "precondition_sha256": item.precondition_sha256,
                }
                for item in items
            ]
        )
    )


async def artifact_rows(database: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await database.execute("SELECT * FROM agent_artifacts ORDER BY artifact_id")
    return list(await cursor.fetchall())


async def request_rows(database: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await database.execute(
        "SELECT * FROM protocol_requests ORDER BY client_instance_id,request_id"
    )
    return list(await cursor.fetchall())


async def save_plan(
    database: aiosqlite.Connection,
    plan: MaintenancePlan,
    items: list[PlanItem],
) -> None:
    payload = plan.model_dump_json(warnings="error")
    await database.execute(
        "INSERT INTO store_maintenance_plans VALUES (?,?,?,?)",
        (str(plan.plan_id), payload, digest(payload), plan.created_at.isoformat()),
    )
    await database.executemany(
        "INSERT INTO store_maintenance_items VALUES (?,?,?,?,?,?)",
        [
            (
                str(plan.plan_id),
                ordinal,
                item.kind,
                item.key_json,
                item.key_sha256,
                item.precondition_sha256,
            )
            for ordinal, item in enumerate(items)
        ],
    )
    await database.execute(
        "INSERT INTO store_maintenance_progress "
        "(plan_id,state,next_ordinal,applied_items,skipped_items,backup_sha256,started_at,"
        "updated_at,completed_at) VALUES (?, 'planned', 0, 0, 0, NULL, NULL, ?, NULL)",
        (str(plan.plan_id), plan.created_at.isoformat()),
    )


async def load_plan_items(
    database: aiosqlite.Connection, plan_id: UUID
) -> tuple[MaintenancePlan, list[PlanItem]]:
    cursor = await database.execute(
        "SELECT * FROM store_maintenance_plans WHERE plan_id=?", (str(plan_id),)
    )
    row = await cursor.fetchone()
    if row is None:
        raise KernelError("maintenance_plan_not_found", "维护计划不存在")
    plan = _validate_plan(row, plan_id)
    item_cursor = await database.execute(
        "SELECT * FROM store_maintenance_items WHERE plan_id=? ORDER BY ordinal",
        (str(plan_id),),
    )
    items = [
        _validate_item(item_row, ordinal)
        for ordinal, item_row in enumerate(await item_cursor.fetchall())
    ]
    if candidate_set_sha256(items) != plan.candidate_set_sha256:
        raise KernelError("maintenance_corrupt", "维护候选集合摘要损坏")
    return plan, items


def _validate_plan(row: aiosqlite.Row, plan_id: UUID) -> MaintenancePlan:
    payload = row["payload_json"]
    if not isinstance(payload, str) or digest(payload) != row["payload_sha256"]:
        raise KernelError("maintenance_corrupt", "维护计划摘要损坏")
    try:
        plan = MaintenancePlan.model_validate_json(payload)
    except ValidationError:
        raise KernelError("maintenance_corrupt", "维护计划结构损坏") from None
    if plan.plan_id != plan_id:
        raise KernelError("maintenance_corrupt", "维护计划身份损坏")
    return plan


def _validate_item(row: aiosqlite.Row, ordinal: int) -> PlanItem:
    if row["ordinal"] != ordinal or digest(row["key_json"]) != row["key_sha256"]:
        raise KernelError("maintenance_corrupt", "维护候选顺序或摘要损坏")
    try:
        key: Any = json.loads(row["key_json"])
    except (json.JSONDecodeError, TypeError):
        raise KernelError("maintenance_corrupt", "维护候选身份损坏") from None
    if not isinstance(key, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in key.items()
    ):
        raise KernelError("maintenance_corrupt", "维护候选身份损坏")
    return PlanItem(row["kind"], key, row["precondition_sha256"])


async def load_progress(
    database: aiosqlite.Connection, plan_id: UUID, total_items: int
) -> MaintenanceProgress:
    cursor = await database.execute(
        "SELECT * FROM store_maintenance_progress WHERE plan_id=?", (str(plan_id),)
    )
    row = await cursor.fetchone()
    if row is None:
        raise KernelError("maintenance_corrupt", "维护进度缺失")
    try:
        return MaintenanceProgress(
            plan_id=plan_id,
            state=row["state"],
            total_items=total_items,
            next_ordinal=row["next_ordinal"],
            applied_items=row["applied_items"],
            skipped_items=row["skipped_items"],
            backup_sha256=row["backup_sha256"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
    except ValidationError:
        raise KernelError("maintenance_corrupt", "维护进度结构损坏") from None
