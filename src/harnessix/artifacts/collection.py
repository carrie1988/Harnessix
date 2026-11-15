"""过期Artifact正文的有界回收事务：清空正文、保留Tombstone，活动或无法核对的行保留。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.artifacts.contracts import ArtifactRef, CollectionReport
from harnessix.artifacts.persistence import ARTIFACT_READ_SELECT
from harnessix.session.sqlite import SQLiteSessionStore


async def collect_expired(
    session: SQLiteSessionStore,
    reference: Callable[[aiosqlite.Row, Thread], ArtifactRef],
    fault: Callable[[str], None],
    *,
    now: datetime,
    limit: int,
    after: UUID | None,
) -> CollectionReport:
    """按Artifact ID扫描一批到期Published行，在同一写事务里回收可回收的行。"""
    expired = protected = 0
    async with session._connection() as database:
        await database.execute("BEGIN IMMEDIATE")
        cursor = await database.execute(
            ARTIFACT_READ_SELECT + " WHERE state = 'published' AND expires_at <= ? "
            "AND artifact_id > ? ORDER BY artifact_id LIMIT ?",
            (now.isoformat(), str(after) if after else "", limit),
        )
        rows = list(await cursor.fetchall())
        threads: dict[str, Thread | None] = {}
        for row in rows:
            owner = row["thread_id"]
            if owner not in threads:
                threads[owner] = await session._snapshot(database, UUID(owner))
            thread = threads[owner]
            if not _reclaimable(row, thread, reference):
                protected += 1
                continue
            await database.execute(
                "UPDATE agent_artifacts SET state = 'expired', body = NULL WHERE artifact_id = ?",
                (row["artifact_id"],),
            )
            expired += 1
        fault("artifact.before_collect_commit")
        await database.commit()
    return CollectionReport(
        len(rows),
        expired,
        protected,
        now,
        UUID(rows[-1]["artifact_id"]) if len(rows) == limit else None,
    )


def _reclaimable(
    row: aiosqlite.Row,
    thread: Thread | None,
    reference: Callable[[aiosqlite.Row, Thread], ArtifactRef],
) -> bool:
    """所属Thread有活跃Turn时保护；归属或Manifest无法核对的行保守保留，不中止整批（ADR 0164）。"""
    if thread is None:
        return False
    try:
        reference(row, thread)
    except KernelError as error:
        if error.code != "artifact_unreferenced":
            return False
    return thread.active_turn_id is None
