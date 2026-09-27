"""认证Session的SQLite侧表与原字节读取；不拥有事务提交或密钥托管生命周期。"""

from __future__ import annotations

import hashlib
import hmac
from time import monotonic
from uuid import UUID

import aiosqlite

from harnessix.agent.cancellation import parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, Thread
from harnessix.session.store_publication import (
    EMPTY_PREFIX,
    MAX_HISTORY_BYTES,
    MAX_HISTORY_EVENTS,
    MAX_PROJECTION_BYTES,
    ProjectionPublicationSeal,
    SessionPublicationBinding,
    extend_prefix,
    original_bytes,
    unproven,
)


async def verify_store(
    database: aiosqlite.Connection,
    publication: SessionPublicationBinding | None,
    *,
    enroll: bool = False,
) -> None:
    cursor = await database.execute(
        "SELECT name FROM sqlite_master WHERE name='agent_publication_store'"
    )
    if await cursor.fetchone() is None:
        if publication is not None:
            raise unproven()
        return
    cursor = await database.execute(
        "SELECT substr(seal,1,4097) AS seal FROM agent_publication_store WHERE singleton=1"
    )
    row = await cursor.fetchone()
    if row is None:
        if publication is None:
            return
        if not enroll:
            raise unproven()
        for table in (
            "agent_events",
            "agent_threads",
            "agent_artifacts",
            "agent_event_publications",
            "agent_projection_publications",
        ):
            cursor = await database.execute("SELECT 1 FROM " + table + " LIMIT 1")
            if await cursor.fetchone() is not None:
                raise unproven()
        await database.execute(
            "INSERT INTO agent_publication_store VALUES (1,?)", (publication.header(),)
        )
    elif publication is None:
        raise KernelError("publication_key_unavailable", "认证Session必须提供独立持久密钥")
    else:
        publication.verify_header(row["seal"])


async def checkpoint(
    database: aiosqlite.Connection, publication: SessionPublicationBinding, thread_id: UUID
) -> ProjectionPublicationSeal | None:
    cursor = await database.execute(
        "SELECT substr(seal,1,4097) AS seal FROM agent_projection_publications WHERE thread_id=?",
        (str(thread_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        cursor = await database.execute(
            "SELECT 1 FROM agent_events WHERE thread_id=? LIMIT 1", (str(thread_id),)
        )
        if await cursor.fetchone() is not None:
            raise unproven()
        return None
    return publication.verify_projection(row["seal"], thread_id)


async def verify_snapshot(
    database: aiosqlite.Connection,
    publication: SessionPublicationBinding | None,
    thread_id: UUID,
    row: aiosqlite.Row | None,
) -> None:
    if publication is None:
        return
    await verify_store(database, publication)
    proof = await checkpoint(database, publication, thread_id)
    if row is None:
        if proof is not None:
            raise unproven()
        return
    if proof is None or row["projection_version"] != 20 or row["sequence"] != proof.sequence:
        raise unproven()
    digest = hashlib.sha256(original_bytes(row["snapshot_json"], MAX_PROJECTION_BYTES)).hexdigest()
    if not hmac.compare_digest(proof.snapshot_sha256, digest):
        raise unproven()
    cursor = await database.execute(
        "SELECT COUNT(*) FROM agent_events e JOIN agent_event_publications p "
        "ON e.event_id=p.event_id WHERE e.thread_id=?",
        (str(thread_id),),
    )
    count = await cursor.fetchone()
    if count is None or count[0] != proof.sequence:
        raise unproven()
    cursor = await database.execute(
        "SELECT p.prefix_sha256 FROM agent_events e JOIN agent_event_publications p "
        "ON e.event_id=p.event_id WHERE e.thread_id=? AND e.sequence=?",
        (str(thread_id), proof.sequence),
    )
    tail = await cursor.fetchone()
    if tail is None or tail[0] != proof.prefix_sha256:
        raise unproven()


async def verified_event(
    database: aiosqlite.Connection, publication: SessionPublicationBinding, row: aiosqlite.Row
) -> bytes:
    cursor = await database.execute(
        "SELECT substr(seal,1,4097) AS seal FROM agent_event_publications WHERE event_id=?",
        (row["event_id"],),
    )
    sealed = await cursor.fetchone()
    if sealed is None:
        raise unproven()
    try:
        publication.verify_event(
            sealed["seal"],
            original_bytes(row["event_json"], 1024 * 1024),
            UUID(row["event_id"]),
            UUID(row["thread_id"]),
            row["sequence"],
        )
    except (ValueError, TypeError):
        raise unproven() from None
    seal = sealed["seal"]
    if not isinstance(seal, bytes):
        raise unproven()
    return seal


async def persist_event(
    database: aiosqlite.Connection,
    publication: SessionPublicationBinding | None,
    event: AgentEvent,
    prefix: str,
    history_bytes: int,
) -> tuple[str, int]:
    if publication is None:
        body, seal = event.model_dump_json().encode(), None
    else:
        body, seal = await publication.issue_event(event)
    if seal is not None:
        history_bytes += len(body) + len(seal)
        if event.sequence > MAX_HISTORY_EVENTS or history_bytes > MAX_HISTORY_BYTES:
            raise KernelError("publication_history_limit", "Session历史认证超过资源上限")
    await database.execute(
        "INSERT INTO agent_events VALUES (?,?,?,?)",
        (str(event.thread_id), event.sequence, str(event.event_id), body.decode()),
    )
    if seal is not None:
        prefix = extend_prefix(prefix, seal)
        await database.execute(
            "INSERT INTO agent_event_publications VALUES (?,?,?)",
            (str(event.event_id), prefix, seal),
        )
    return prefix, history_bytes


async def persist_projection(
    database: aiosqlite.Connection,
    publication: SessionPublicationBinding | None,
    thread: Thread,
    encoded: str,
    prefix: str,
    history_bytes: int,
) -> None:
    if publication is not None:
        await database.execute(
            "INSERT INTO agent_projection_publications VALUES (?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET seal=excluded.seal",
            (str(thread.thread_id), publication.projection(thread, encoded, prefix, history_bytes)),
        )


async def authenticated_events(
    database: aiosqlite.Connection,
    publication: SessionPublicationBinding,
    thread_id: UUID,
    after: int,
) -> list[AgentEvent]:
    """从根重算完整前缀再返回选定事件；损坏投影不改变原事件认证依据。"""
    await verify_store(database, publication)
    proof = await checkpoint(database, publication, thread_id)
    if proof is None:
        return []
    deadline, size = monotonic() + 10.0, 0

    def check() -> None:
        if monotonic() >= deadline:
            raise KernelError("publication_history_timeout", "Session历史认证超时")

    check_cancel = parent_cancel_checkpointer(check)
    cursor = await database.execute(
        "SELECT thread_id,sequence,event_id,substr(event_json,1,1048577) AS event_json "
        "FROM agent_events WHERE thread_id=? ORDER BY sequence",
        (str(thread_id),),
    )
    prefix, count, selected = EMPTY_PREFIX, 0, []
    async for row in cursor:
        check_cancel()
        count += 1
        if count > MAX_HISTORY_EVENTS:
            raise KernelError("publication_history_limit", "Session历史认证超过资源上限")
        if row["sequence"] != count:
            raise unproven()
        seal = await verified_event(database, publication, row)
        size += len(row["event_json"].encode()) + len(seal)
        if size > MAX_HISTORY_BYTES:
            raise KernelError("publication_history_limit", "Session历史认证超过资源上限")
        prefix = extend_prefix(prefix, seal)
        # MAC成功后才允许反序列化事件，索引与原Body身份也必须一致。
        event = AgentEvent.model_validate_json(row["event_json"])
        if (
            str(event.thread_id) != row["thread_id"]
            or str(event.event_id) != row["event_id"]
            or event.sequence != count
        ):
            raise unproven()
        if count > after:
            selected.append(event)
        check_cancel()
    if count != proof.sequence or prefix != proof.prefix_sha256 or size != proof.history_bytes:
        raise unproven()
    return selected


async def save_projection(
    database: aiosqlite.Connection,
    publication: SessionPublicationBinding | None,
    thread: Thread,
    prefix: str | None,
    history_bytes: int | None,
) -> None:
    """原投影与认证Checkpoint共用调用方事务；Rebuild复用已认证原前缀。"""
    encoded = thread.model_dump_json()
    await database.execute(
        "INSERT INTO agent_threads "
        "(thread_id, sequence, snapshot_json, snapshot_sha256, projection_version) "
        "VALUES (?, ?, ?, ?, 20) "
        "ON CONFLICT(thread_id) DO UPDATE SET sequence = excluded.sequence, "
        "snapshot_json = excluded.snapshot_json, snapshot_sha256 = excluded.snapshot_sha256, "
        "projection_version = excluded.projection_version",
        (
            str(thread.thread_id),
            thread.sequence,
            encoded,
            hashlib.sha256(encoded.encode()).hexdigest(),
        ),
    )
    if publication is not None:
        if prefix is None or history_bytes is None:
            proof = await checkpoint(database, publication, thread.thread_id)
            if proof is None:
                raise unproven()
            prefix, history_bytes = proof.prefix_sha256, proof.history_bytes
        await persist_projection(database, publication, thread, encoded, prefix, history_bytes)
