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
    source_thread_id = UUID(sys.argv[2])
    operation = sys.argv[3]
    point = sys.argv[4]

    def crash(name: str) -> None:
        if name == point:
            os._exit(91)

    store = SQLiteSessionStore(path, fault=crash)
    async with AgentRuntime(store, FakeProvider()) as runtime:
        if operation == "fork":
            await runtime.fork_thread(source_thread_id, request_id="crash-fork")
        else:
            await runtime.archive_thread(source_thread_id, reason="崩溃测试")


asyncio.run(main())
