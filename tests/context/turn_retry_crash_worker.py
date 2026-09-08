from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


async def main() -> None:
    path = Path(sys.argv[1])
    thread_id = UUID(sys.argv[2])
    source_turn_id = UUID(sys.argv[3])
    point = sys.argv[4]

    def crash(name: str) -> None:
        if name == point:
            os._exit(92)

    store = SQLiteSessionStore(path, fault=crash)
    async with AgentRuntime(store, FakeProvider()) as runtime:
        await runtime.retry_turn(thread_id, source_turn_id, request_id="crash-retry")


asyncio.run(main())
