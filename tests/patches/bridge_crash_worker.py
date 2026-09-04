"""测试子进程：宿主夹具持久归属，不冒充尚未实现的 Agent 写审批事件。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.patches import managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from tests.patches.bridge_helpers import approval


async def main():
    root, workspace_id, fixture, cut = sys.argv[1:]
    path = Path(fixture)
    data = json.loads(await asyncio.to_thread(path.read_text))
    call = ToolCallContent.model_validate_json(json.dumps(data["call"]))
    scope = ToolExecutionScope(
        UUID(data["thread_id"]),
        UUID(data["turn_id"]),
        call.call_id,
        data["workspace"],
        data["call_fingerprint"],
    )
    managed._fault = lambda at: os._exit(75) if at == cut else None
    with managed.PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
        original_save, original_reply = copy.save, copy.reply

        def save(*args):
            result = original_save(*args)
            if cut == "plan_saved":
                os._exit(75)
            return result

        def reply(*args):
            result = original_reply(*args)
            if cut == "decision_mirrored":
                os._exit(75)
            return result

        copy.save, copy.reply = save, reply
        async with ManagedPatchBridge(copy) as bridge:
            plan = await bridge.prepare(call, scope, CancelToken())
            await asyncio.to_thread(path.with_suffix(".plan").write_text, plan.model_dump_json())
            await bridge.execute(call, scope, plan, approval(plan), CancelToken())
            if cut == "bridge_returned":
                os._exit(75)
    raise AssertionError("未到达进程退出切点")


if __name__ == "__main__":
    asyncio.run(main())
