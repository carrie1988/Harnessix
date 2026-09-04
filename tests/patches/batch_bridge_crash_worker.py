"""真实退出宿主夹具；不把夹具文件冒充 Session 持久审批。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.patches import batch_execution, managed, managed_batches
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
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
    index = -1

    def single_fault(at):
        nonlocal index
        if at == "started":
            index += 1
        if cut == f"{at}:{index}":
            os._exit(78)

    def fault(at):
        if at == cut.removeprefix("recover:"):
            os._exit(78)

    managed._fault = single_fault
    batch_execution._fault = fault
    managed_batches._fault = lambda at: (
        os._exit(78)
        if (
            (at == "reservation_committed" and cut == "plan_saved")
            or (at == "approval_committed" and cut == "decision_mirrored")
        )
        else None
    )
    with managed.PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
        async with ManagedPatchBatchBridge(copy) as bridge:
            if cut.startswith("recover:"):
                plan = ManagedPatchBatchCallPlan.model_validate_json(
                    path.with_suffix(".plan").read_text()
                )
                await bridge.recover(call, scope, CancelToken(), plan=plan, approval=approval(plan))
            else:
                plan = await bridge.prepare(call, scope, CancelToken())
                await asyncio.to_thread(
                    path.with_suffix(".plan").write_text, plan.model_dump_json()
                )
                await bridge.execute(call, scope, plan, approval(plan), CancelToken())
                fault("bridge_returned")
    raise AssertionError("未到达退出切点")


if __name__ == "__main__":
    asyncio.run(main())
