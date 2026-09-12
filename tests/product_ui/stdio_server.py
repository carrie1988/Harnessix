"""为Product UI纵向测试提供真实JSONL子进程，不参与发行物构建。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.models.scripted import FakeProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore


async def _serve(database: Path, workspace: Path) -> None:
    sessions = SQLiteSessionStore(database)
    workspace_root = await asyncio.to_thread(workspace.resolve, strict=True)
    async with AgentRuntime(sessions, FakeProvider("stdio恢复完成")) as runtime:
        service = AgentApplicationService(
            runtime,
            sessions,
            SQLiteProtocolRequestStore(sessions.path),
            workspace=workspace_root,
        )
        await run_stdio(
            AgentProtocolServer(service),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )


if __name__ == "__main__":
    asyncio.run(_serve(Path(sys.argv[1]), Path(sys.argv[2])))
