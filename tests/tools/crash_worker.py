"""只读工具真实进程故障夹具；计数文件是测试插桩，不是工具产品能力。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.tools.test_search_kernel import tool_step


async def main():
    database, thread, root, point, count, tool, mode = sys.argv[1:]

    class ObservedTools(CodingToolRuntime):
        async def execute(self, call, cancel):
            result = await super().execute(call, cancel)
            if result.outcome == "succeeded":
                await asyncio.to_thread(record_read, Path(count))
            return result

    def crash(name):
        if name == point:
            os._exit(77)

    async with ObservedTools(Path(root)) as tools:
        options = {"scoped_tools": tools} if mode == "scoped" else {"tools": tools}
        async with AgentRuntime(
            SQLiteSessionStore(database),
            ScriptedProvider([tool_step(tool), answer()]),
            fault=crash,
            **options,
        ) as runtime:
            await runtime.run_turn(UUID(thread), "只读进程故障", request_id="crash")
    raise AssertionError("故障点未触发")


def record_read(path: Path) -> None:
    count = int(path.read_text()) if path.exists() else 0
    path.write_text(str(count + 1))


if __name__ == "__main__":
    asyncio.run(main())
