"""真实 Session × 副本组账本退出；恢复路径禁止重新准备或执行。"""

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import batch_execution, managed, managed_batches
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import batch_step, decide
from tests.patches.test_managed_batches import prepare


async def main():
    root, workspace_id, database, cut = sys.argv[1:]
    index = -1

    def fault(at):
        if at == cut.removeprefix("recover:"):
            os._exit(79)

    def single(at):
        nonlocal index
        if at == "started":
            index += 1
        fault(f"{at}:{index}")

    managed._fault = single
    batch_execution._fault = fault
    managed_batches._fault = fault
    with managed.PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
        async with ManagedPatchBatchBridge(copy) as bridge:
            if cut.startswith("recover:"):
                async with AgentRuntime(
                    SQLiteSessionStore(Path(database)),
                    ScriptedProvider([]),
                    patch_batches=bridge,
                    fault=fault,
                ):
                    pass
            else:
                async with AgentRuntime(
                    SQLiteSessionStore(Path(database)),
                    ScriptedProvider([batch_step(copy, bridge, prepare(copy)), answer()]),
                    patch_batches=bridge,
                    fault=fault,
                ) as runtime:
                    thread = await runtime.create_thread(str(copy.workspace.root))
                    waiting = await runtime.run_turn(
                        thread.thread_id, "真实整组组合崩溃", request_id="crash"
                    )
                    await decide(runtime, thread.thread_id, waiting)
                    await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    raise AssertionError("未到达退出切点")


if __name__ == "__main__":
    asyncio.run(main())
