"""执行既有Process Action并在子进程启动后真实退出。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from harnessix.domain.registry import ToolRegistry
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.storage import SQLiteEffectJournal
from harnessix.worker import ActionWorker
from tests.processes.helpers import _ready


async def main() -> None:
    database, root, marker = map(Path, sys.argv[1:4])
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(database),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        lease_seconds=1,
        auto_execute=False,
    )
    await service.initialize()
    execution = asyncio.create_task(
        ActionWorker(
            service,
            poll_seconds=0.01,
            heartbeat_seconds=0.2,
            recovery_interval_seconds=1,
        ).run_once()
    )
    for _ in range(500):
        if await asyncio.to_thread(_ready, marker) is not None:
            os._exit(89)
        if execution.done():
            await execution
            raise AssertionError("Process Action在硬退出窗口前结束")
        await asyncio.sleep(0.01)
    raise AssertionError("未到达Process Action硬退出窗口")


if __name__ == "__main__":
    asyncio.run(main())
