import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.workspace import ReadOperation
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.bridge_helpers import approval
from tests.patches.test_batch_diff_bridge import ledger_state
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot


@pytest.mark.parametrize(
    "view,cut",
    [(v, c) for v in ("plan", "effect") for c in ("before", "during", "after")]
    + [("effect", "unfinished")],
)
async def test_real_exit_does_not_mutate_ledger_or_targets(group_case, tmp_path, view, cut):
    source, factory, copy, _, prepared = group_case
    original_source = snapshot(source.root)
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan)
        result = (
            await bridge.execute(call, scope, plan, decision, CancelToken())
            if view == "effect" and cut != "unfinished"
            else None
        )
    before = ledger_state(copy), snapshot(copy.workspace.root)
    root = factory.root
    data = {
        "root": str(root),
        "workspace_id": str(copy.workspace_id),
        "call": call.model_dump_json(),
        "plan": plan.model_dump_json(),
        "approval": decision.model_dump_json(),
        "execution": result.execution.model_dump_json() if result else None,
    }
    metadata = tmp_path / "diff-crash.json"
    metadata.write_text(json.dumps(data))
    copy.close()
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.patches.batch_diff_crash_worker", str(metadata), cut, view],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=Path(__file__).parents[2],
    )
    assert child.returncode == 81, child.stderr
    with factory.open(plan.backend.workspace_id) as reopened:
        after = ledger_state(reopened), snapshot(reopened.workspace.root)
        assert after[1] == before[1]
        async with ManagedPatchBatchBridge(reopened) as bridge:
            if cut == "unfinished":
                actual = ManagedPatchBatches(reopened).get_execution(
                    plan.backend.batch_id, ReadOperation()
                )
                assert actual.run.phase == "started"
                with pytest.raises(KernelError) as error:
                    await bridge.diff(
                        call,
                        scope,
                        plan,
                        CancelToken(),
                        view="effect",
                        approval=decision,
                        execution=actual,
                    )
                assert error.value.code == "patch_diff_effect_unsettled"
            else:
                assert after == before
                report = await bridge.diff(
                    call,
                    scope,
                    plan,
                    CancelToken(),
                    view=view,
                    approval=decision if view == "effect" else None,
                    execution=result.execution if result else None,
                )
                assert report.document.summary.complete
        assert (ledger_state(reopened), snapshot(reopened.workspace.root)) == after
    assert snapshot(source.root) == original_source
