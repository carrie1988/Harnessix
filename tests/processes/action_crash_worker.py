"""Action Plane硬退出夹具；父测试负责回收仍存活的测试进程组。"""

import asyncio
import os
import sys
from pathlib import Path

from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.domain.registry import ToolRegistry
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.storage import SQLiteEffectJournal
from tests.helpers import action_request
from tests.processes.helpers import _ready


async def main() -> None:
    database, root, marker, action_marker = map(Path, sys.argv[1:5])
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(database),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        lease_seconds=1,
    )
    await service.initialize()
    request = action_request(
        "host.process",
        {
            "program": "python",
            "arguments": [
                "-I",
                "-c",
                "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)",
                str(marker),
            ],
            "timeout_seconds": 60.0,
        },
        idempotency_key="process:hard-exit",
    )
    await service.submit(request)
    action_marker.write_text(str(request.action_id))
    execution = asyncio.create_task(
        service.decide_approval(
            request.action_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="crash-fixture"),
        )
    )
    for _ in range(500):
        if await asyncio.to_thread(_ready, marker) is not None:
            os._exit(84)
        if execution.done():
            await execution
            raise AssertionError("进程过早结束")
        await asyncio.sleep(0.01)
    raise AssertionError("未到达Action硬退出窗口")


if __name__ == "__main__":
    asyncio.run(main())
