"""维护计划执行：每批业务变更与进度游标在同一事务提交。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.session.capacity import ThreadFact, load_thread_facts, thread_has_uncertain_effect
from harnessix.session.maintenance_contracts import MaintenancePlan
from harnessix.session.maintenance_records import (
    PlanItem,
    artifact_precondition,
    load_plan_items,
    load_progress,
    parse_time,
    request_precondition,
    thread_precondition,
)
from harnessix.session.sqlite import SQLiteSessionStore


async def run_batches(
    session: SQLiteSessionStore,
    plan_id: UUID,
    *,
    batch_size: int,
    owner: object,
    fault: Callable[[str], None],
) -> None:
    while True:
        completed = await _run_one_batch(session, plan_id, batch_size=batch_size, owner=owner)
        fault("maintenance.after_batch_commit")
        if completed:
            return


async def _run_one_batch(
    session: SQLiteSessionStore,
    plan_id: UUID,
    *,
    batch_size: int,
    owner: object,
) -> bool:
    async with session._connection() as database:
        await database.execute("BEGIN IMMEDIATE")
        plan, items = await load_plan_items(database, plan_id)
        progress = await load_progress(database, plan_id, len(items))
        if progress.state == "completed":
            await database.commit()
            return True
        if progress.state != "running":
            raise KernelError("maintenance_state_conflict", "维护计划尚未启动")
        batch = items[progress.next_ordinal : progress.next_ordinal + batch_size]
        applied = sum([await apply_item(database, plan, item) for item in batch])
        next_ordinal = progress.next_ordinal + len(batch)
        completed = next_ordinal == len(items)
        await _advance_progress(
            database,
            plan_id,
            previous=progress.next_ordinal,
            next_ordinal=next_ordinal,
            applied=applied,
            skipped=len(batch) - applied,
            completed=completed,
        )
        if session._runtime_owner_token is not owner:
            raise KernelError("maintenance_runtime_required", "Store维护执行期间宿主已关闭")
        await database.commit()
        return completed


async def _advance_progress(
    database: aiosqlite.Connection,
    plan_id: UUID,
    *,
    previous: int,
    next_ordinal: int,
    applied: int,
    skipped: int,
    completed: bool,
) -> None:
    now = datetime.now(UTC).isoformat()
    updated = await database.execute(
        "UPDATE store_maintenance_progress SET state=?,next_ordinal=?,"
        "applied_items=applied_items+?,skipped_items=skipped_items+?,updated_at=?,"
        "completed_at=? WHERE plan_id=? AND state='running' AND next_ordinal=?",
        (
            "completed" if completed else "running",
            next_ordinal,
            applied,
            skipped,
            now,
            now if completed else None,
            str(plan_id),
            previous,
        ),
    )
    if updated.rowcount != 1:
        raise KernelError("maintenance_state_conflict", "维护进度并发变化")


async def apply_item(
    database: aiosqlite.Connection,
    plan: MaintenancePlan,
    item: PlanItem,
) -> int:
    if item.kind == "artifact_body":
        return await _expire_artifact(database, plan, item)
    if item.kind == "protocol_request":
        return await _delete_protocol_request(database, plan, item)
    if item.kind == "session_thread":
        return await _delete_session_thread(database, plan, item)
    raise KernelError("maintenance_corrupt", "维护候选类型无效")


async def _expire_artifact(
    database: aiosqlite.Connection,
    plan: MaintenancePlan,
    item: PlanItem,
) -> int:
    cursor = await database.execute(
        "SELECT * FROM agent_artifacts WHERE artifact_id=?", (item.key["artifact_id"],)
    )
    row = await cursor.fetchone()
    if row is None or row["state"] == "expired":
        return 0
    if (
        artifact_precondition(row) != item.precondition_sha256
        or parse_time(row["expires_at"], code="artifact_corrupt") > plan.policy.cutoff
    ):
        return 0
    accepted = await database.execute(
        "SELECT 1 FROM protocol_requests WHERE state='accepted' LIMIT 1"
    )
    if await accepted.fetchone() is not None:
        return 0
    facts = await load_thread_facts(database)
    fact = next((fact for fact in facts if str(fact.thread.thread_id) == row["thread_id"]), None)
    if (
        fact is None
        or fact.thread.active_turn_id is not None
        or thread_has_uncertain_effect(fact.thread)
    ):
        return 0
    updated = await database.execute(
        "UPDATE agent_artifacts SET state='expired',body=NULL "
        "WHERE artifact_id=? AND state='published'",
        (item.key["artifact_id"],),
    )
    return int(updated.rowcount == 1)


async def _delete_protocol_request(
    database: aiosqlite.Connection,
    plan: MaintenancePlan,
    item: PlanItem,
) -> int:
    cursor = await database.execute(
        "SELECT * FROM protocol_requests WHERE client_instance_id=? AND request_id=?",
        (item.key["client_instance_id"], item.key["request_id"]),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    if (
        row["state"] == "accepted"
        or request_precondition(row) != item.precondition_sha256
        or parse_time(row["updated_at"], code="request_corrupt") > plan.policy.cutoff
    ):
        return 0
    deleted = await database.execute(
        "DELETE FROM protocol_requests WHERE client_instance_id=? AND request_id=? "
        "AND state IN ('completed','failed')",
        (item.key["client_instance_id"], item.key["request_id"]),
    )
    return int(deleted.rowcount == 1)


async def _delete_session_thread(
    database: aiosqlite.Connection,
    plan: MaintenancePlan,
    item: PlanItem,
) -> int:
    facts = await load_thread_facts(database)
    identity = item.key["thread_id"]
    fact = next(
        (candidate for candidate in facts if str(candidate.thread.thread_id) == identity), None
    )
    if fact is None or _thread_is_protected(fact, facts, plan, item):
        return 0
    accepted = await database.execute(
        "SELECT 1 FROM protocol_requests WHERE state='accepted' LIMIT 1"
    )
    if await accepted.fetchone() is not None:
        return 0
    live_artifact = await database.execute(
        "SELECT 1 FROM agent_artifacts WHERE thread_id=? AND state='published' LIMIT 1",
        (identity,),
    )
    if await live_artifact.fetchone() is not None:
        return 0
    await database.execute("DELETE FROM agent_artifacts WHERE thread_id=?", (identity,))
    await database.execute("DELETE FROM agent_events WHERE thread_id=?", (identity,))
    deleted = await database.execute(
        "DELETE FROM agent_threads WHERE thread_id=? AND sequence=?",
        (identity, fact.thread.sequence),
    )
    return int(deleted.rowcount == 1)


def _thread_is_protected(
    fact: ThreadFact,
    facts: tuple[ThreadFact, ...],
    plan: MaintenancePlan,
    item: PlanItem,
) -> bool:
    thread = fact.thread
    return (
        thread_precondition(fact) != item.precondition_sha256
        or thread.archive is None
        or thread.archive.archived_at > plan.policy.cutoff
        or thread.active_turn_id is not None
        or thread_has_uncertain_effect(thread)
        or any(
            candidate.thread.fork_snapshot is not None
            and str(candidate.thread.fork_snapshot.source_thread_id) == str(thread.thread_id)
            for candidate in facts
        )
    )
