from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite
from pydantic import BaseModel

from harnessix.domain.errors import (
    ActionConflictError,
    ActionNotFoundError,
    IdempotencyConflictError,
    IllegalTransitionError,
)
from harnessix.domain.models import (
    ALLOWED_ACTION_TRANSITIONS,
    ActionEvent,
    ActionRequest,
    ActionResult,
    ActionSnapshot,
    ActionStatus,
    ApprovalRecord,
    PolicyDecision,
    ToolDescriptor,
    utc_now,
)


def _json_dump(value: object) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _iso(value: datetime) -> str:
    return value.isoformat()


class SQLiteEffectJournal:
    """SQLite Action 快照和追加式 Effect Journal。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        migration = files("harnessix.storage.migrations").joinpath("0001_initial.sql").read_text()
        async with self._connection() as database:
            await database.executescript(migration)
            await database.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (_iso(utc_now()),),
            )
            await database.commit()

    async def close(self) -> None:
        """SQLite 实现按操作创建连接，无常驻资源需要释放。"""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        database = await aiosqlite.connect(self.database_path)
        database.row_factory = aiosqlite.Row
        await database.execute("PRAGMA foreign_keys = ON")
        await database.execute("PRAGMA busy_timeout = 5000")
        try:
            yield database
        finally:
            await database.close()

    async def create_action(
        self,
        request: ActionRequest,
        tool: ToolDescriptor,
        request_fingerprint: str,
    ) -> tuple[ActionSnapshot, bool]:
        now = utc_now()
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            existing_by_id = await self._fetch_one(
                database, "SELECT * FROM actions WHERE action_id = ?", (str(request.action_id),)
            )
            if existing_by_id is not None:
                stored_request = ActionRequest.model_validate_json(existing_by_id["request_json"])
                if stored_request != request:
                    raise ActionConflictError("相同 action_id 已绑定到不同的请求载荷")
                await database.commit()
                return self._snapshot(existing_by_id), False

            if request.idempotency_key is not None:
                existing_by_key = await self._fetch_one(
                    database,
                    "SELECT * FROM actions WHERE tenant_id = ? AND idempotency_key = ?",
                    (request.principal.tenant_id, request.idempotency_key),
                )
                if existing_by_key is not None:
                    if existing_by_key["request_fingerprint"] != request_fingerprint:
                        raise IdempotencyConflictError
                    await database.commit()
                    return self._snapshot(existing_by_key), False

            await database.execute(
                """
                INSERT INTO actions(
                    action_id, tenant_id, idempotency_key, request_fingerprint,
                    request_json, tool_json, status, created_at, updated_at, version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    str(request.action_id),
                    request.principal.tenant_id,
                    request.idempotency_key,
                    request_fingerprint,
                    _json_dump(request),
                    _json_dump(tool),
                    ActionStatus.RECEIVED.value,
                    _iso(now),
                    _iso(now),
                ),
            )
            await self._insert_event(
                database,
                action_id=request.action_id,
                event_type="action_received",
                from_status=None,
                to_status=ActionStatus.RECEIVED,
                data={"request_fingerprint": request_fingerprint},
                created_at=now,
            )
            await database.commit()
            row = await self._require_row(database, request.action_id)
            return self._snapshot(row), True

    async def get_action(self, action_id: UUID | str) -> ActionSnapshot:
        async with self._connection() as database:
            return self._snapshot(await self._require_row(database, action_id))

    async def list_events(self, action_id: UUID | str) -> list[ActionEvent]:
        async with self._connection() as database:
            await self._require_row(database, action_id)
            cursor = await database.execute(
                "SELECT * FROM action_events WHERE action_id = ? ORDER BY sequence",
                (str(action_id),),
            )
            rows = await cursor.fetchall()
            return [self._event(row) for row in rows]

    async def transition(
        self,
        action_id: UUID | str,
        *,
        expected: Iterable[ActionStatus],
        target: ActionStatus,
        event_type: str,
        data: dict[str, Any] | None = None,
        policy: PolicyDecision | None = None,
        approval: ApprovalRecord | None = None,
        result: ActionResult | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        clear_lease: bool = False,
        required_lease_owner: str | None = None,
    ) -> ActionSnapshot:
        expected_set = frozenset(expected)
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            row = await self._require_row(database, action_id)
            now = utc_now()
            current = ActionStatus(row["status"])
            if current not in expected_set:
                raise IllegalTransitionError(current, target)
            if target not in ALLOWED_ACTION_TRANSITIONS[current]:
                raise IllegalTransitionError(current, target)
            if required_lease_owner is not None and not self._has_valid_lease(
                row, required_lease_owner, now
            ):
                raise ActionConflictError("执行租约所有者不匹配或租约已经过期")

            assignments = ["status = ?", "updated_at = ?", "version = version + 1"]
            values: list[object] = [target.value, _iso(now)]
            if policy is not None:
                assignments.append("policy_json = ?")
                values.append(_json_dump(policy))
            if approval is not None:
                assignments.append("approval_json = ?")
                values.append(_json_dump(approval))
            if result is not None:
                assignments.append("result_json = ?")
                values.append(_json_dump(result))
            if lease_owner is not None:
                assignments.append("lease_owner = ?")
                values.append(lease_owner)
            if lease_expires_at is not None:
                assignments.append("lease_expires_at = ?")
                values.append(_iso(lease_expires_at))
            if clear_lease:
                assignments.extend(["lease_owner = NULL", "lease_expires_at = NULL"])

            values.append(str(action_id))
            await database.execute(
                f"UPDATE actions SET {', '.join(assignments)} WHERE action_id = ?", values
            )
            await self._insert_event(
                database,
                action_id=action_id,
                event_type=event_type,
                from_status=current,
                to_status=target,
                data=data or {},
                created_at=now,
            )
            await database.commit()
            return self._snapshot(await self._require_row(database, action_id))

    async def claim_next_ready(
        self, *, worker_id: str, lease_expires_at: datetime
    ) -> ActionSnapshot | None:
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            row = await self._fetch_one(
                database,
                """
                SELECT * FROM actions
                WHERE status = ?
                ORDER BY created_at, action_id
                LIMIT 1
                """,
                (ActionStatus.READY.value,),
            )
            if row is None:
                await database.commit()
                return None

            now = utc_now()
            action_id = UUID(row["action_id"])
            await database.execute(
                """
                UPDATE actions
                SET status = ?, lease_owner = ?, lease_expires_at = ?,
                    updated_at = ?, version = version + 1
                WHERE action_id = ?
                """,
                (
                    ActionStatus.LEASED.value,
                    worker_id,
                    _iso(lease_expires_at),
                    _iso(now),
                    str(action_id),
                ),
            )
            await self._insert_event(
                database,
                action_id=action_id,
                event_type="execution_leased",
                from_status=ActionStatus.READY,
                to_status=ActionStatus.LEASED,
                data={"worker_id": worker_id},
                created_at=now,
            )
            await database.commit()
            return self._snapshot(await self._require_row(database, action_id))

    async def renew_lease(
        self,
        action_id: UUID | str,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        async with self._connection() as database:
            now = utc_now()
            cursor = await database.execute(
                """
                UPDATE actions
                SET lease_expires_at = ?, updated_at = ?, version = version + 1
                WHERE action_id = ?
                  AND lease_owner = ?
                  AND lease_expires_at > ?
                  AND status IN (?, ?, ?)
                """,
                (
                    _iso(lease_expires_at),
                    _iso(now),
                    str(action_id),
                    worker_id,
                    _iso(now),
                    ActionStatus.LEASED.value,
                    ActionStatus.RUNNING.value,
                    ActionStatus.RECONCILING.value,
                ),
            )
            await database.commit()
            return cursor.rowcount == 1

    async def recover_expired(self, now: datetime | None = None) -> list[UUID]:
        recovery_time = now or utc_now()
        recovered: list[UUID] = []
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            cursor = await database.execute(
                """
                SELECT * FROM actions
                WHERE status IN (?, ?, ?)
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (
                    ActionStatus.LEASED.value,
                    ActionStatus.RUNNING.value,
                    ActionStatus.RECONCILING.value,
                    _iso(recovery_time),
                ),
            )
            rows = await cursor.fetchall()
            for row in rows:
                action_id = UUID(row["action_id"])
                current = ActionStatus(row["status"])
                target = (
                    ActionStatus.READY if current is ActionStatus.LEASED else ActionStatus.UNKNOWN
                )
                await database.execute(
                    """
                    UPDATE actions
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = ?, version = version + 1
                    WHERE action_id = ?
                    """,
                    (target.value, _iso(recovery_time), str(action_id)),
                )
                await self._insert_event(
                    database,
                    action_id=action_id,
                    event_type="lease_recovered",
                    from_status=current,
                    to_status=target,
                    data={"reason": "执行租约已过期"},
                    created_at=recovery_time,
                )
                recovered.append(action_id)
            await database.commit()
        return recovered

    async def _insert_event(
        self,
        database: aiosqlite.Connection,
        *,
        action_id: UUID | str,
        event_type: str,
        from_status: ActionStatus | None,
        to_status: ActionStatus,
        data: dict[str, Any],
        created_at: datetime,
    ) -> None:
        cursor = await database.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM action_events WHERE action_id = ?",
            (str(action_id),),
        )
        sequence_row = await cursor.fetchone()
        assert sequence_row is not None
        sequence = int(sequence_row[0])
        await database.execute(
            """
            INSERT INTO action_events(
                action_id, sequence, event_type, from_status, to_status, data_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(action_id),
                sequence,
                event_type,
                from_status.value if from_status is not None else None,
                to_status.value,
                _json_dump(data),
                _iso(created_at),
            ),
        )

    async def _require_row(
        self, database: aiosqlite.Connection, action_id: UUID | str
    ) -> aiosqlite.Row:
        row = await self._fetch_one(
            database, "SELECT * FROM actions WHERE action_id = ?", (str(action_id),)
        )
        if row is None:
            raise ActionNotFoundError(action_id)
        return row

    @staticmethod
    async def _fetch_one(
        database: aiosqlite.Connection, sql: str, values: tuple[object, ...]
    ) -> aiosqlite.Row | None:
        cursor = await database.execute(sql, values)
        return await cursor.fetchone()

    @staticmethod
    def _snapshot(row: aiosqlite.Row) -> ActionSnapshot:
        return ActionSnapshot(
            request=ActionRequest.model_validate_json(row["request_json"]),
            request_fingerprint=row["request_fingerprint"],
            tool=ToolDescriptor.model_validate_json(row["tool_json"]),
            status=ActionStatus(row["status"]),
            policy=(
                PolicyDecision.model_validate_json(row["policy_json"])
                if row["policy_json"]
                else None
            ),
            approval=(
                ApprovalRecord.model_validate_json(row["approval_json"])
                if row["approval_json"]
                else None
            ),
            result=(
                ActionResult.model_validate_json(row["result_json"]) if row["result_json"] else None
            ),
            lease_owner=row["lease_owner"],
            lease_expires_at=(
                datetime.fromisoformat(row["lease_expires_at"]) if row["lease_expires_at"] else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _event(row: aiosqlite.Row) -> ActionEvent:
        return ActionEvent(
            action_id=UUID(row["action_id"]),
            sequence=int(row["sequence"]),
            event_type=row["event_type"],
            from_status=(ActionStatus(row["from_status"]) if row["from_status"] else None),
            to_status=ActionStatus(row["to_status"]),
            data=json.loads(row["data_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _has_valid_lease(row: aiosqlite.Row, worker_id: str, now: datetime) -> bool:
        expires_at = row["lease_expires_at"]
        return bool(
            row["lease_owner"] == worker_id
            and expires_at
            and datetime.fromisoformat(expires_at) > now
        )
