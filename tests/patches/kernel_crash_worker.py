"""真实 Kernel/Session 与受管副本组合退出，不使用宿主审批夹具代替事件。"""

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.patches.test_kernel_patch import decide, patch_step


async def main():
    root, workspace_id, database, cut = sys.argv[1:]

    def fault(at):
        if at == cut:
            os._exit(76)

    managed._fault = fault
    with managed.PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
        original_reply = copy.reply

        def reply(*args):
            result = original_reply(*args)
            fault("decision_mirrored")
            return result

        copy.reply = reply
        async with ManagedPatchBridge(copy) as bridge:
            async with AgentRuntime(
                SQLiteSessionStore(Path(database)),
                ScriptedProvider([patch_step(copy, bridge), answer()]),
                patches=bridge,
                fault=fault,
            ) as runtime:
                thread = await runtime.create_thread(str(copy.workspace.root))
                waiting = await runtime.run_turn(thread.thread_id, "组合崩溃", request_id="crash")
                await decide(runtime, thread.thread_id, waiting)
                await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    raise AssertionError("未到达进程退出切点")


if __name__ == "__main__":
    asyncio.run(main())
