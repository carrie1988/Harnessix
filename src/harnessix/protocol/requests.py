from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

MAX_REQUEST_OUTCOME_BYTES = 1_048_576


class ProtocolRequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    client_instance_id: UUID
    request_id: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=128)
    params_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["accepted", "completed", "failed"]
    outcome: JsonValue = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ProtocolRequestClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record: ProtocolRequestRecord
    created: bool


class ProtocolRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProtocolRequestStore(Protocol):
    async def claim(
        self,
        client_instance_id: UUID,
        request_id: str,
        method: str,
        params: JsonValue,
    ) -> ProtocolRequestClaim: ...

    async def complete(
        self, client_instance_id: UUID, request_id: str, outcome: JsonValue
    ) -> ProtocolRequestRecord: ...

    async def fail(
        self, client_instance_id: UUID, request_id: str, outcome: JsonValue
    ) -> ProtocolRequestRecord: ...

    async def get(
        self, client_instance_id: UUID, request_id: str
    ) -> ProtocolRequestRecord | None: ...


def _json(value: JsonValue) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise ProtocolRequestError("invalid_outcome", "协议请求数据不是有效JSON") from None
    if len(encoded.encode()) > MAX_REQUEST_OUTCOME_BYTES:
        raise ProtocolRequestError("outcome_too_large", "协议请求结果超过持久化限制")
    return encoded


def request_fingerprint(method: str, params: JsonValue) -> str:
    if not method or len(method) > 128:
        raise ProtocolRequestError("invalid_request", "协议方法名长度无效")
    return hashlib.sha256(f"{method}\n{_json(params)}".encode()).hexdigest()


class SQLiteProtocolRequestStore:
    """与Session共库的协议命令账本；只保存参数摘要和有界公开结果。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            await database.execute("PRAGMA foreign_keys = ON")
            await database.execute("PRAGMA busy_timeout = 5000")
            try:
                yield database
            except BaseException:
                await database.rollback()
                raise

    @staticmethod
    def _validate_identity(client_instance_id: UUID, request_id: str) -> None:
        if type(request_id) is not str or not 1 <= len(request_id) <= 256:
            raise ProtocolRequestError("invalid_request", "requestId长度无效")
        if not isinstance(client_instance_id, UUID):
            raise ProtocolRequestError("invalid_request", "clientInstanceId无效")

    @staticmethod
    def _record(row: aiosqlite.Row) -> ProtocolRequestRecord:
        outcome_json: str | None = row["outcome_json"]
        outcome_sha256: str | None = row["outcome_sha256"]
        if (outcome_json is None) != (outcome_sha256 is None):
            raise ProtocolRequestError("request_corrupt", "协议请求结果摘要不完整")
        if outcome_json is not None:
            if hashlib.sha256(outcome_json.encode()).hexdigest() != outcome_sha256:
                raise ProtocolRequestError("request_corrupt", "协议请求结果摘要校验失败")
            try:
                outcome = json.loads(outcome_json)
            except (json.JSONDecodeError, RecursionError):
                raise ProtocolRequestError("request_corrupt", "协议请求结果JSON损坏") from None
        else:
            outcome = None
        try:
            return ProtocolRequestRecord.model_validate_json(
                json.dumps(
                    {
                        "client_instance_id": row["client_instance_id"],
                        "request_id": row["request_id"],
                        "method": row["method"],
                        "params_sha256": row["params_sha256"],
                        "state": row["state"],
                        "outcome": outcome,
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (ValidationError, TypeError, ValueError):
            raise ProtocolRequestError("request_corrupt", "协议请求记录损坏") from None

    async def _get(
        self, database: aiosqlite.Connection, client_instance_id: UUID, request_id: str
    ) -> ProtocolRequestRecord | None:
        cursor = await database.execute(
            "SELECT * FROM protocol_requests WHERE client_instance_id=? AND request_id=?",
            (str(client_instance_id), request_id),
        )
        row = await cursor.fetchone()
        return None if row is None else self._record(row)

    async def get(self, client_instance_id: UUID, request_id: str) -> ProtocolRequestRecord | None:
        self._validate_identity(client_instance_id, request_id)
        async with self._connection() as database:
            return await self._get(database, client_instance_id, request_id)

    async def claim(
        self,
        client_instance_id: UUID,
        request_id: str,
        method: str,
        params: JsonValue,
    ) -> ProtocolRequestClaim:
        self._validate_identity(client_instance_id, request_id)
        fingerprint = request_fingerprint(method, params)
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            existing = await self._get(database, client_instance_id, request_id)
            if existing is not None:
                if existing.method != method or existing.params_sha256 != fingerprint:
                    raise ProtocolRequestError(
                        "idempotency_conflict", "requestId已绑定其他协议命令"
                    )
                await database.commit()
                return ProtocolRequestClaim(record=existing, created=False)
            now = datetime.now(UTC).isoformat()
            await database.execute(
                "INSERT INTO protocol_requests "
                "(client_instance_id,request_id,method,params_sha256,state,outcome_json,"
                "outcome_sha256,created_at,updated_at) VALUES (?,?,?,?,?,NULL,NULL,?,?)",
                (
                    str(client_instance_id),
                    request_id,
                    method,
                    fingerprint,
                    "accepted",
                    now,
                    now,
                ),
            )
            await database.commit()
        record = await self.get(client_instance_id, request_id)
        assert record is not None
        return ProtocolRequestClaim(record=record, created=True)

    async def _finish(
        self,
        client_instance_id: UUID,
        request_id: str,
        state: Literal["completed", "failed"],
        outcome: JsonValue,
    ) -> ProtocolRequestRecord:
        self._validate_identity(client_instance_id, request_id)
        encoded = _json(outcome)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self._connection() as database:
            await database.execute("BEGIN IMMEDIATE")
            existing = await self._get(database, client_instance_id, request_id)
            if existing is None:
                raise ProtocolRequestError("request_not_found", "协议请求尚未受理")
            if existing.state != "accepted":
                if existing.state == state and existing.outcome == outcome:
                    await database.commit()
                    return existing
                raise ProtocolRequestError("request_state_conflict", "协议请求终态冲突")
            await database.execute(
                "UPDATE protocol_requests SET state=?,outcome_json=?,outcome_sha256=?,"
                "updated_at=? WHERE client_instance_id=? AND request_id=? AND state='accepted'",
                (
                    state,
                    encoded,
                    digest,
                    datetime.now(UTC).isoformat(),
                    str(client_instance_id),
                    request_id,
                ),
            )
            await database.commit()
        record = await self.get(client_instance_id, request_id)
        assert record is not None
        return record

    async def complete(
        self, client_instance_id: UUID, request_id: str, outcome: JsonValue
    ) -> ProtocolRequestRecord:
        return await self._finish(client_instance_id, request_id, "completed", outcome)

    async def fail(
        self, client_instance_id: UUID, request_id: str, outcome: JsonValue
    ) -> ProtocolRequestRecord:
        return await self._finish(client_instance_id, request_id, "failed", outcome)
