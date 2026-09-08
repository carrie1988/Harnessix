"""模型历史准备提交前后的真实进程退出夹具。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import step


async def main():
    database, thread_id, root, point = sys.argv[1:]
    provider = ScriptedProvider([step(query="needle", max_results=40), answer()])

    def fault(name):
        if name == point and len(provider.requests) == 1:
            os._exit(88)

    store = SQLiteSessionStore(database)
    artifacts = SQLiteArtifactStore(store)
    async with CodingToolRuntime(Path(root), artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            fault=fault,
            tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=2048),
        ) as runtime:
            await runtime.run_turn(UUID(thread_id), "历史准备退出", request_id="history-crash")
    raise AssertionError("未到达模型历史故障点")


if __name__ == "__main__":
    asyncio.run(main())
