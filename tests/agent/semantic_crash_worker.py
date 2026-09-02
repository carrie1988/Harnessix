from __future__ import annotations

import asyncio
import os
import sys
from uuid import UUID

from harnessix.agent.errors import AgentFailure
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    CompactionContent,
    ErrorContent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PlanContent,
    PlanStep,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.session.sqlite import SQLiteSessionStore


async def main() -> None:
    database, thread_id, kind, point = sys.argv[1:]

    def crash(name):
        if name == point:
            os._exit(77)

    store = SQLiteSessionStore(database, fault=crash)
    await store.initialize()
    thread = await store.get_thread(UUID(thread_id))
    content = {
        "plan": PlanContent(steps=(PlanStep(step_id="a", description="验证"),)),
        "context_compaction": CompactionContent(
            source_item_ids=tuple(i.item_id for i in thread.turns[0].items),
            summary="旧消息摘要",
            tokens_before=20,
            tokens_after=2,
            tokenizer="fixture",
        ),
        "error": ErrorContent(failure=AgentFailure(code="budget_exceeded", message="预算耗尽")),
    }[kind]
    item_id = new_id()
    payloads = [
        ItemStarted(item_id=item_id, content=content),
        ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
    ]
    if isinstance(content, ErrorContent):
        payloads.append(TurnStateChanged(status=TurnStatus.FAILED, error=content.failure))
    await store.append(
        thread.thread_id,
        [EventDraft(turn_id=thread.active_turn_id, payload=p) for p in payloads],
        expected_sequence=thread.sequence,
    )
    raise AssertionError("故障点未触发")


if __name__ == "__main__":
    asyncio.run(main())
