"""在实际报告生成和未结算组运行中退出；不模拟 Session 归档提交。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import ApprovalRecord
from harnessix.patches import batch_agent_bridge, batch_execution, diff_document
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.batch_run_contracts import BatchExecutionResult
from harnessix.patches.managed import PatchWorkspaces


async def main(data):
    cut, view = sys.argv[2:]
    call = ToolCallContent.model_validate_json(data["call"])
    plan = ManagedPatchBatchCallPlan.model_validate_json(data["plan"])
    decision = ApprovalRecord.model_validate_json(data["approval"])
    execution = (
        BatchExecutionResult.model_validate_json(data["execution"]) if data["execution"] else None
    )
    with PatchWorkspaces(Path(data["root"])).open(UUID(data["workspace_id"])) as copy:
        scope = ToolExecutionScope(
            plan.thread_id,
            plan.turn_id,
            plan.call_id,
            str(copy.workspace.root),
            plan.call_fingerprint,
        )
        async with ManagedPatchBatchBridge(copy) as bridge:
            if cut == "unfinished":

                def stop(point):
                    if point == "run_started":
                        os._exit(81)

                batch_execution._fault = stop
                await bridge.execute(call, scope, plan, decision, CancelToken())
            else:
                original = batch_agent_bridge.batch_diff_document
                edits = diff_document._patch_edits

                def midway(*args):
                    for row in edits(*args):
                        os._exit(81)
                        yield row

                def report(*args, **kwargs):
                    if cut == "before":
                        os._exit(81)
                    value = original(*args, **kwargs)
                    if cut == "after":
                        os._exit(81)
                    return value

                if cut == "during":
                    diff_document._patch_edits = midway
                batch_agent_bridge.batch_diff_document = report
                await bridge.diff(
                    call,
                    scope,
                    plan,
                    CancelToken(),
                    view=view,
                    approval=decision if view == "effect" else None,
                    execution=execution if view == "effect" else None,
                )
    raise AssertionError("未到达真实退出点")


if __name__ == "__main__":
    asyncio.run(main(json.loads(Path(sys.argv[1]).read_text())))
