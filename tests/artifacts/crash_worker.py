"""真实进程退出夹具；只在归档发布事务中武装 Session 故障点。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import step


async def main():
    database, thread, root, point, counter, tool = sys.argv[1:]
    armed = False

    def crash(name):
        nonlocal armed
        if name == "artifact.after_insert":
            armed = True
        if name == point and (not name.startswith("session.") or armed):
            os._exit(77)

    class ObservedTools(CodingToolRuntime):
        async def execute_scoped(self, call, scope, cancel):
            result = await super().execute_scoped(call, scope, cancel)
            assert isinstance(result, ArtifactToolResult)
            await asyncio.to_thread(record, Path(counter))
            return result

    session = SQLiteSessionStore(database, fault=crash)
    artifacts = SQLiteArtifactStore(session, fault=crash)
    args = {"pattern": "*.py"} if tool == "glob" else {"query": "needle"}
    async with ObservedTools(Path(root), artifacts=artifacts) as tools:
        async with AgentRuntime(
            session,
            ScriptedProvider([step(tool, **args), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
            fault=crash,
        ) as runtime:
            await runtime.run_turn(UUID(thread), "事务崩溃", request_id="crash")
    raise AssertionError("故障点未触发")


def record(path):
    path.write_text(str((int(path.read_text()) if path.exists() else 0) + 1))


if __name__ == "__main__":
    asyncio.run(main())
