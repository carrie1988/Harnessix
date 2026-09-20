"""Session共库容量扫描：只输出计数、时间和字节水位。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aiosqlite
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Item, Thread
from harnessix.session.maintenance_contracts import (
    StoreCapacityReport,
    StoreCapacitySnapshot,
)

_UNCERTAIN_EFFECT_STATES = frozenset(
    {
        "pending_approval",
        "ready",
        "leased",
        "running",
        "unknown",
        "reconciling",
        "manual_intervention",
    }
)


@dataclass(frozen=True, slots=True)
class ThreadFact:
    """经事件序号与快照摘要核验的内部Thread事实。"""

    thread: Thread
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class RequestFact:
    state: str
    created_at: datetime


def _parse_time(value: object, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise KernelError(code, "持久记录时间字段损坏")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise KernelError(code, "持久记录时间字段损坏") from None
    if parsed.tzinfo is None:
        raise KernelError(code, "持久记录时间缺少时区")
    return parsed


def _item_has_uncertain_effect(item: Item) -> bool:
    content = item.content
    if getattr(content, "outcome", None) == "unknown":
        return True
    effect = getattr(content, "effect", None)
    status = getattr(effect, "status", None)
    if status is not None and str(status) in _UNCERTAIN_EFFECT_STATES:
        return True
    for name in ("action_status", "route_state"):
        value = getattr(content, name, None)
        if value is not None and str(value) in _UNCERTAIN_EFFECT_STATES:
            return True
    return False


def thread_has_uncertain_effect(thread: Thread) -> bool:
    inherited = thread.fork_snapshot.items if thread.fork_snapshot is not None else ()
    return any(
        _item_has_uncertain_effect(item)
        for item in (*inherited, *(item for turn in thread.turns for item in turn.items))
    )


async def load_thread_facts(database: aiosqlite.Connection) -> tuple[ThreadFact, ...]:
    cursor = await database.execute(
        "SELECT t.thread_id,t.sequence,t.snapshot_json,t.snapshot_sha256,t.projection_version,"
        "COUNT(e.sequence) AS event_count,COALESCE(MAX(e.sequence),0) AS event_last "
        "FROM agent_threads t LEFT JOIN agent_events e ON e.thread_id=t.thread_id "
        "GROUP BY t.thread_id,t.sequence,t.snapshot_json,t.snapshot_sha256,t.projection_version "
        "ORDER BY t.thread_id"
    )
    facts: list[ThreadFact] = []
    for row in await cursor.fetchall():
        encoded = row["snapshot_json"]
        if (
            not isinstance(encoded, str)
            or hashlib.sha256(encoded.encode()).hexdigest() != row["snapshot_sha256"]
            or row["event_count"] != row["sequence"]
            or row["event_last"] != row["sequence"]
        ):
            raise KernelError("projection_corrupt", "Session容量扫描发现投影与事件不一致")
        try:
            thread = Thread.model_validate_json(encoded)
        except ValidationError:
            raise KernelError("projection_corrupt", "Session容量扫描发现投影结构损坏") from None
        if str(thread.thread_id) != row["thread_id"] or thread.sequence != row["sequence"]:
            raise KernelError("projection_corrupt", "Session容量扫描发现投影身份不一致")
        facts.append(ThreadFact(thread=thread, snapshot_sha256=row["snapshot_sha256"]))
    return tuple(facts)


def _time_bounds(values: list[datetime]) -> tuple[datetime | None, datetime | None]:
    return (min(values), max(values)) if values else (None, None)


def _physical_sizes(path: Path) -> tuple[int, int]:
    wal_path = Path(str(path) + "-wal")
    return (
        path.stat().st_size if path.exists() else 0,
        wal_path.stat().st_size if wal_path.exists() else 0,
    )


async def _schema_version(database: aiosqlite.Connection) -> int:
    cursor = await database.execute("SELECT COALESCE(MAX(version),0) FROM agent_migrations")
    row = await cursor.fetchone()
    assert row is not None
    version = row[0]
    if type(version) is not int or version < 1:
        raise KernelError("invalid_migration", "Session Migration版本无效")
    return version


async def _session_capacity(database: aiosqlite.Connection) -> StoreCapacitySnapshot:
    threads = await load_thread_facts(database)
    cursor = await database.execute("SELECT COUNT(*) FROM agent_events")
    row = await cursor.fetchone()
    assert row is not None
    oldest, newest = _time_bounds([fact.thread.created_at for fact in threads])
    return StoreCapacitySnapshot(
        store_kind="session",
        logical_rows_by_kind={"threads": len(threads), "events": row[0]},
        oldest_created_at=oldest,
        newest_created_at=newest,
        active_rows=sum(fact.thread.active_turn_id is not None for fact in threads),
        terminal_rows=sum(fact.thread.archive is not None for fact in threads),
        unknown_rows=sum(thread_has_uncertain_effect(fact.thread) for fact in threads),
    )


def _request_fact(row: aiosqlite.Row) -> RequestFact:
    state = row["state"]
    outcome = row["outcome_json"]
    outcome_sha256 = row["outcome_sha256"]
    try:
        UUID(row["client_instance_id"])
        valid_identity = (
            isinstance(row["request_id"], str)
            and 1 <= len(row["request_id"]) <= 256
            and isinstance(row["method"], str)
            and 1 <= len(row["method"]) <= 128
            and isinstance(row["params_sha256"], str)
            and len(row["params_sha256"]) == 64
        )
        if not valid_identity or state not in {"accepted", "completed", "failed"}:
            raise ValueError("请求身份无效")
        if state == "accepted":
            if outcome is not None or outcome_sha256 is not None:
                raise ValueError("未决请求包含结果")
        elif (
            not isinstance(outcome, str)
            or not isinstance(outcome_sha256, str)
            or hashlib.sha256(outcome.encode()).hexdigest() != outcome_sha256
        ):
            raise ValueError("终态请求结果损坏")
        else:
            json.loads(outcome)
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError):
        raise KernelError("request_corrupt", "协议请求容量扫描发现记录损坏") from None
    created_at = _parse_time(row["created_at"], code="request_corrupt")
    updated_at = _parse_time(row["updated_at"], code="request_corrupt")
    if updated_at < created_at:
        raise KernelError("request_corrupt", "协议请求更新时间早于创建时间")
    return RequestFact(state=state, created_at=created_at)


async def _protocol_capacity(database: aiosqlite.Connection) -> StoreCapacitySnapshot:
    cursor = await database.execute("SELECT * FROM protocol_requests ORDER BY created_at")
    requests = [_request_fact(row) for row in await cursor.fetchall()]
    oldest, newest = _time_bounds([request.created_at for request in requests])
    return StoreCapacitySnapshot(
        store_kind="protocol_request",
        logical_rows_by_kind={
            "requests": len(requests),
            "accepted": sum(request.state == "accepted" for request in requests),
            "completed": sum(request.state == "completed" for request in requests),
            "failed": sum(request.state == "failed" for request in requests),
        },
        oldest_created_at=oldest,
        newest_created_at=newest,
        active_rows=sum(request.state == "accepted" for request in requests),
        terminal_rows=sum(request.state != "accepted" for request in requests),
        unknown_rows=0,
    )


async def _artifact_capacity(database: aiosqlite.Connection) -> StoreCapacitySnapshot:
    cursor = await database.execute(
        "SELECT state,created_at,length(body) AS body_bytes FROM agent_artifacts "
        "ORDER BY created_at"
    )
    rows = list(await cursor.fetchall())
    oldest, newest = _time_bounds(
        [_parse_time(row["created_at"], code="artifact_corrupt") for row in rows]
    )
    return StoreCapacitySnapshot(
        store_kind="artifact",
        logical_rows_by_kind={
            "manifests": len(rows),
            "published": sum(row["state"] == "published" for row in rows),
            "expired": sum(row["state"] == "expired" for row in rows),
        },
        oldest_created_at=oldest,
        newest_created_at=newest,
        active_rows=sum(row["state"] == "published" for row in rows),
        terminal_rows=sum(row["state"] == "expired" for row in rows),
        unknown_rows=0,
        artifact_body_bytes=sum(row["body_bytes"] or 0 for row in rows),
    )


async def capacity_report(
    database: aiosqlite.Connection,
    path: Path,
    *,
    captured_at: datetime | None = None,
) -> StoreCapacityReport:
    """在调用方读事务内重算三类Store逻辑容量。"""

    schema_version = await _schema_version(database)
    session = await _session_capacity(database)
    protocol = await _protocol_capacity(database)
    artifacts = await _artifact_capacity(database)
    database_bytes, wal_bytes = await asyncio.to_thread(_physical_sizes, path)
    return StoreCapacityReport(
        schema_version=schema_version,
        database_bytes=database_bytes,
        wal_bytes=wal_bytes,
        captured_at=captured_at or datetime.now(UTC),
        stores=(session, protocol, artifacts),
    )
