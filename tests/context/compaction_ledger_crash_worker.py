from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.models import EventDraft
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


async def main():
    database, thread_id, payload_path, point = sys.argv[1:]

    def crash(name):
        if name == point:
            os._exit(77)

    store = SQLiteSessionStore(database, fault=crash)
    if payload_path == "recovery":
        async with AgentRuntime(store, FakeProvider()):
            pass
    else:
        thread = await store.get_thread(UUID(thread_id))
        event = EventDraft.model_validate_json(
            await asyncio.to_thread(Path(payload_path).read_bytes)
        )
        await store.append(thread.thread_id, [event], expected_sequence=thread.sequence)
    raise AssertionError("未触发退出切点")


if __name__ == "__main__":
    asyncio.run(main())
