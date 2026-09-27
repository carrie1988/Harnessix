"""Session新事件CAS和同事务认证提交；保留原幂等事实，禁止重签重复事件。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, EventDraft, Thread
from harnessix.agent.reducer import apply_event
from harnessix.session.sqlite_publication import checkpoint, persist_event, verified_event
from harnessix.session.store_publication import EMPTY_PREFIX, SessionPublicationBinding


class AppendStore(Protocol):
    """仅同事务写入需要的内部端口，不扩大公共SessionStore合同。"""

    _publication: SessionPublicationBinding | None
    _fault: Callable[[str], None]

    async def _snapshot(self, database: aiosqlite.Connection, thread_id: UUID) -> Thread | None: ...
    def _parse_event(self, row: aiosqlite.Row) -> AgentEvent: ...
    async def _save(
        self,
        database: aiosqlite.Connection,
        thread: Thread,
        *,
        prefix: str | None = None,
        history_bytes: int | None = None,
    ) -> None: ...


async def append_in_transaction(
    store: AppendStore,
    database: aiosqlite.Connection,
    thread_id: UUID,
    batch: tuple[EventDraft, ...],
    expected_sequence: int,
) -> tuple[Thread, bool]:
    """先验证重复事实与原Snapshot，再对真正新事件逐个保护并提交证明。"""
    matched: list[AgentEvent] = []
    for draft in batch:
        fields = (
            "thread_id,sequence,event_id,substr(event_json,1,1048577) AS event_json"
            if store._publication is not None
            else "*"
        )
        cursor = await database.execute(
            "SELECT " + fields + " FROM agent_events WHERE event_id=?", (str(draft.event_id),)
        )
        row = await cursor.fetchone()
        if row is not None:
            if store._publication is not None:
                await verified_event(database, store._publication, row)
            event = store._parse_event(row)
            stored = EventDraft.model_validate(event.model_dump(exclude={"thread_id", "sequence"}))
            if stored != draft or event.thread_id != thread_id:
                raise KernelError("event_conflict", "同一事件 ID 已绑定不同载荷")
            matched.append(event)
    if matched:
        if len(matched) != len(batch) or any(
            event.sequence != expected_sequence + index for index, event in enumerate(matched, 1)
        ):
            raise KernelError("event_conflict", "事件批次部分重复或顺序冲突")
        thread = await store._snapshot(database, thread_id)
        assert thread is not None
        return thread, False
    thread = await store._snapshot(database, thread_id)
    sequence = thread.sequence if thread else 0
    if sequence != expected_sequence:
        raise KernelError("sequence_conflict", "Thread 已更新，请重新读取 sequence")
    prefix, history_bytes = EMPTY_PREFIX, 0
    if store._publication is not None and thread is not None:
        proof = await checkpoint(database, store._publication, thread_id)
        assert proof is not None
        prefix, history_bytes = proof.prefix_sha256, proof.history_bytes
    for draft in batch:
        sequence += 1
        event = AgentEvent(**draft.model_dump(), thread_id=thread_id, sequence=sequence)
        thread = apply_event(thread, event)
        prefix, history_bytes = await persist_event(
            database, store._publication, event, prefix, history_bytes
        )
    assert thread is not None
    store._fault("session.after_events")
    await store._save(database, thread, prefix=prefix, history_bytes=history_bytes)
    store._fault("session.after_projection")
    return thread, True
