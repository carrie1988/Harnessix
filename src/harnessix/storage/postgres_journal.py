from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from importlib.resources import files
from typing import Any
from uuid import UUID

import asyncpg
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
    JournalOperationalStats,
    PolicyDecision,
    ToolDescriptor,
    TraceContext,
    utc_now,
)

_MIGRATION_LOCK_ID = 7_214_559_001


def _json_dump(value: object) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PostgresEffectJournal:
    """支持多 Worker 并发 Claim 的 PostgreSQL Effect Journal。"""

    def __init__(self, database_url: str, *, pool_size: int = 10) -> None:
        if not database_url:
            raise ValueError("database_url 不能为空")
        if pool_size <= 0:
            raise ValueError("pool_size 必须大于 0")
        self.database_url = database_url
        self.pool_size = pool_size
        self._pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._initialize_lock:
            try:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        dsn=self.database_url,
                        min_size=1,
                        max_size=self.pool_size,
                    )
                async with self._pool.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_ID
                        )
                        await connection.execute(
                            """
                            CREATE TABLE IF NOT EXISTS schema_migrations (
                                version INTEGER PRIMARY KEY,
                                applied_at TIMESTAMPTZ NOT NULL
                            )
                            """,
                        )
                        rows = await connection.fetch("SELECT version FROM schema_migrations")
                        applied_versions = {int(row["version"]) for row in rows}
                        migration_root = files("harnessix.storage.migrations.postgresql")
                        migrations = sorted(
                            (
                                migration
                                for migration in migration_root.iterdir()
                                if migration.name.endswith(".sql")
                            ),
                            key=lambda migration: migration.name,
                        )
                        for migration in migrations:
                            version = int(migration.name.split("_", maxsplit=1)[0])
                            if version in applied_versions:
                                continue
                            await connection.execute(migration.read_text())
                            await connection.execute(
                                """
                                INSERT INTO schema_migrations(version, applied_at)
                                VALUES($1, $2)
                                """,
                                version,
                                utc_now(),
                            )
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    async def ping(self) -> bool:
        pool = self._pool
        if pool is None:
            return False
        try:
            async with pool.acquire() as connection:
                return bool(await connection.fetchval("SELECT 1"))
        except (asyncpg.PostgresError, OSError):
            return False

    async def operational_stats(self) -> JournalOperationalStats:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = $1) AS ready_count,
                    COUNT(*) FILTER (WHERE status = $2) AS pending_approval_count,
                    COUNT(*) FILTER (WHERE status = $3) AS unknown_count,
                    MIN(created_at) FILTER (WHERE status = $1) AS oldest_ready_at
                FROM actions
                """,
                ActionStatus.READY.value,
                ActionStatus.PENDING_APPROVAL.value,
                ActionStatus.UNKNOWN.value,
            )
            assert row is not None
            return JournalOperationalStats(
                ready_count=int(row["ready_count"]),
                pending_approval_count=int(row["pending_approval_count"]),
                unknown_count=int(row["unknown_count"]),
                oldest_ready_at=row["oldest_ready_at"],
            )

    async def create_action(
        self,
        request: ActionRequest,
        tool: ToolDescriptor,
        request_fingerprint: str,
        trace_context: TraceContext | None = None,
    ) -> tuple[ActionSnapshot, bool]:
        pool = self._require_pool()
        now = utc_now()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO actions(
                        action_id, tenant_id, idempotency_key, request_fingerprint,
                        request_json, tool_json, trace_context_json, status,
                        created_at, updated_at, version
                    ) VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, 1)
                    ON CONFLICT DO NOTHING
                    RETURNING *
                    """,
                    request.action_id,
                    request.principal.tenant_id,
                    request.idempotency_key,
                    request_fingerprint,
                    _json_dump(request),
                    _json_dump(tool),
                    _json_dump(trace_context) if trace_context is not None else None,
                    ActionStatus.RECEIVED.value,
                    now,
                )
                if row is not None:
                    await self._insert_event(
                        connection,
                        action_id=request.action_id,
                        event_type="action_received",
                        from_status=None,
                        to_status=ActionStatus.RECEIVED,
                        data={"request_fingerprint": request_fingerprint},
                        created_at=now,
                    )
                    return self._snapshot(row), True

                existing_by_id = await connection.fetchrow(
                    "SELECT * FROM actions WHERE action_id = $1", request.action_id
                )
                if existing_by_id is not None:
                    stored_request = ActionRequest.model_validate_json(
                        existing_by_id["request_json"]
                    )
                    if stored_request != request:
                        raise ActionConflictError("相同 action_id 已绑定到不同的请求载荷")
                    return self._snapshot(existing_by_id), False

                existing_by_key = await connection.fetchrow(
                    """
                    SELECT * FROM actions
                    WHERE tenant_id = $1 AND idempotency_key = $2
                    """,
                    request.principal.tenant_id,
                    request.idempotency_key,
                )
                if existing_by_key is None:
                    raise ActionConflictError("Action 创建冲突，但没有找到冲突记录")
                if existing_by_key["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflictError
                return self._snapshot(existing_by_key), False

    async def get_action(self, action_id: UUID | str) -> ActionSnapshot:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            return self._snapshot(await self._require_row(connection, action_id))

    async def list_events(self, action_id: UUID | str) -> list[ActionEvent]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            await self._require_row(connection, action_id)
            rows = await connection.fetch(
                "SELECT * FROM action_events WHERE action_id = $1 ORDER BY sequence",
                self._uuid(action_id),
            )
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
        pool = self._require_pool()
        expected_set = frozenset(expected)
        action_uuid = self._uuid(action_id)
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await self._require_row(connection, action_uuid, for_update=True)
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

                updated = await connection.fetchrow(
                    """
                    UPDATE actions
                    SET status = $2,
                        policy_json = COALESCE($3, policy_json),
                        approval_json = COALESCE($4, approval_json),
                        result_json = COALESCE($5, result_json),
                        lease_owner = CASE
                            WHEN $8 THEN NULL
                            WHEN $6::text IS NOT NULL THEN $6::text
                            ELSE lease_owner
                        END,
                        lease_expires_at = CASE
                            WHEN $8 THEN NULL
                            WHEN $7::timestamptz IS NOT NULL THEN $7::timestamptz
                            ELSE lease_expires_at
                        END,
                        updated_at = $9,
                        version = version + 1
                    WHERE action_id = $1
                    RETURNING *
                    """,
                    action_uuid,
                    target.value,
                    _json_dump(policy) if policy is not None else None,
                    _json_dump(approval) if approval is not None else None,
                    _json_dump(result) if result is not None else None,
                    lease_owner,
                    lease_expires_at,
                    clear_lease,
                    now,
                )
                assert updated is not None
                await self._insert_event(
                    connection,
                    action_id=action_uuid,
                    event_type=event_type,
                    from_status=current,
                    to_status=target,
                    data=data or {},
                    created_at=now,
                )
                return self._snapshot(updated)

    async def claim_next_ready(
        self, *, worker_id: str, lease_expires_at: datetime
    ) -> ActionSnapshot | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT * FROM actions
                    WHERE status = $1
                    ORDER BY created_at, action_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    ActionStatus.READY.value,
                )
                if row is None:
                    return None
                now = utc_now()
                action_id = row["action_id"]
                updated = await connection.fetchrow(
                    """
                    UPDATE actions
                    SET status = $2, lease_owner = $3, lease_expires_at = $4,
                        updated_at = $5, version = version + 1
                    WHERE action_id = $1
                    RETURNING *
                    """,
                    action_id,
                    ActionStatus.LEASED.value,
                    worker_id,
                    lease_expires_at,
                    now,
                )
                assert updated is not None
                await self._insert_event(
                    connection,
                    action_id=action_id,
                    event_type="execution_leased",
                    from_status=ActionStatus.READY,
                    to_status=ActionStatus.LEASED,
                    data={"worker_id": worker_id},
                    created_at=now,
                )
                return self._snapshot(updated)

    async def renew_lease(
        self,
        action_id: UUID | str,
        *,
        worker_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            now = utc_now()
            command_status = str(
                await connection.execute(
                    """
                UPDATE actions
                SET lease_expires_at = $3, updated_at = $4, version = version + 1
                WHERE action_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > $4
                  AND status = ANY($5::text[])
                """,
                    self._uuid(action_id),
                    worker_id,
                    lease_expires_at,
                    now,
                    [
                        ActionStatus.LEASED.value,
                        ActionStatus.RUNNING.value,
                        ActionStatus.RECONCILING.value,
                    ],
                )
            )
            return command_status == "UPDATE 1"

    async def recover_expired(self, now: datetime | None = None) -> list[UUID]:
        pool = self._require_pool()
        recovery_time = now or utc_now()
        recovered: list[UUID] = []
        async with pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    """
                    SELECT * FROM actions
                    WHERE status = ANY($1::text[])
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= $2
                    ORDER BY lease_expires_at, action_id
                    FOR UPDATE SKIP LOCKED
                    """,
                    [
                        ActionStatus.LEASED.value,
                        ActionStatus.RUNNING.value,
                        ActionStatus.RECONCILING.value,
                    ],
                    recovery_time,
                )
                for row in rows:
                    action_id = row["action_id"]
                    current = ActionStatus(row["status"])
                    target = (
                        ActionStatus.READY
                        if current is ActionStatus.LEASED
                        else ActionStatus.UNKNOWN
                    )
                    await connection.execute(
                        """
                        UPDATE actions
                        SET status = $2, lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = $3, version = version + 1
                        WHERE action_id = $1
                        """,
                        action_id,
                        target.value,
                        recovery_time,
                    )
                    await self._insert_event(
                        connection,
                        action_id=action_id,
                        event_type="lease_recovered",
                        from_status=current,
                        to_status=target,
                        data={"reason": "执行租约已过期"},
                        created_at=recovery_time,
                    )
                    recovered.append(action_id)
        return recovered

    async def _insert_event(
        self,
        connection: asyncpg.Connection,
        *,
        action_id: UUID,
        event_type: str,
        from_status: ActionStatus | None,
        to_status: ActionStatus,
        data: dict[str, Any],
        created_at: datetime,
    ) -> None:
        sequence = await connection.fetchval(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM action_events
            WHERE action_id = $1
            """,
            action_id,
        )
        await connection.execute(
            """
            INSERT INTO action_events(
                action_id, sequence, event_type, from_status, to_status, data_json, created_at
            ) VALUES($1, $2, $3, $4, $5, $6, $7)
            """,
            action_id,
            sequence,
            event_type,
            from_status.value if from_status is not None else None,
            to_status.value,
            _json_dump(data),
            created_at,
        )

    async def _require_row(
        self,
        connection: asyncpg.Connection,
        action_id: UUID | str,
        *,
        for_update: bool = False,
    ) -> asyncpg.Record:
        suffix = " FOR UPDATE" if for_update else ""
        row = await connection.fetchrow(
            f"SELECT * FROM actions WHERE action_id = $1{suffix}", self._uuid(action_id)
        )
        if row is None:
            raise ActionNotFoundError(action_id)
        return row

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresEffectJournal 尚未初始化")
        return self._pool

    @staticmethod
    def _uuid(action_id: UUID | str) -> UUID:
        return action_id if isinstance(action_id, UUID) else UUID(action_id)

    @staticmethod
    def _snapshot(row: Mapping[str, Any]) -> ActionSnapshot:
        return ActionSnapshot(
            request=ActionRequest.model_validate_json(row["request_json"]),
            request_fingerprint=row["request_fingerprint"],
            tool=ToolDescriptor.model_validate_json(row["tool_json"]),
            status=ActionStatus(row["status"]),
            trace_context=(
                TraceContext.model_validate_json(row["trace_context_json"])
                if row["trace_context_json"]
                else None
            ),
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
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
        )

    @staticmethod
    def _event(row: Mapping[str, Any]) -> ActionEvent:
        return ActionEvent(
            action_id=row["action_id"],
            sequence=int(row["sequence"]),
            event_type=row["event_type"],
            from_status=(ActionStatus(row["from_status"]) if row["from_status"] else None),
            to_status=ActionStatus(row["to_status"]),
            data=json.loads(row["data_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _has_valid_lease(row: Mapping[str, Any], worker_id: str, now: datetime) -> bool:
        expires_at = row["lease_expires_at"]
        return bool(row["lease_owner"] == worker_id and expires_at and expires_at > now)
