"""在真实报告和 Session 联合事务处退出；重开不重写文件。"""

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.batch_diff import SQLiteBatchDiffPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.managed import PatchWorkspaces
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import batch_step, decide
from tests.patches.test_managed_batches import prepare


async def main():
    root, workspace_id, database, view, point = sys.argv[1:]
    count = 0

    def fault(at):
        nonlocal count
        if at == "batch_diff." + point:
            count += 1
            if count == (1 if view == "plan" else 2):
                os._exit(82)

    store = SQLiteSessionStore(database)
    artifacts = SQLiteArtifactStore(store, fault=fault)
    with PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
        async with ManagedPatchBatchBridge(copy) as bridge:
            async with AgentRuntime(
                store,
                ScriptedProvider([batch_step(copy, bridge, prepare(copy)), answer()]),
                patch_batches=bridge,
                batch_diffs=SQLiteBatchDiffPublisher(artifacts, bridge),
            ) as runtime:
                thread = await runtime.create_thread(str(copy.workspace.root))
                waiting = await runtime.run_turn(thread.thread_id, "事务退出", request_id="crash")
                await decide(runtime, thread.thread_id, waiting)
                await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    raise AssertionError("未到达退出点")


if __name__ == "__main__":
    asyncio.run(main())
