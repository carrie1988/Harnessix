import asyncio
import json
import subprocess
import sys

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.patches import batch_agent_bridge
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.managed_batches import ManagedPatchBatches
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.bridge_helpers import approval
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case

CUTS = [
    "plan_saved",
    "decision_mirrored",
    "run_before_commit",
    "run_started",
    *(f"member_approved:{i}" for i in range(3)),
    *(f"{at}:{i}" for i in range(3) for at in ("before_replace", "after_replace")),
    "run_result_before_commit",
    "run_result_committed",
    "bridge_returned",
]


async def child(factory, workspace_id, fixture, cut):
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.patches.batch_bridge_crash_worker",
            str(factory.root),
            str(workspace_id),
            str(fixture),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 78, result.stderr


@pytest.mark.parametrize("cut", [*CUTS, *(f"recover:member_reconciled:{i}" for i in range(3))])
async def test_real_exit_same_call_observation_only(group_case, tmp_path, monkeypatch, cut):
    source, factory, copy, _, prepared = group_case
    original_source = snapshot(source.root)
    bridge = ManagedPatchBatchBridge(copy)
    call, scope = make_call(copy, bridge, prepared)
    workspace_id, target = copy.workspace_id, copy.workspace.root
    fixture = tmp_path / "call.json"
    fixture.write_text(
        json.dumps(
            {
                "call": call.model_dump(mode="json"),
                "thread_id": str(scope.thread_id),
                "turn_id": str(scope.turn_id),
                "workspace": scope.workspace,
                "call_fingerprint": scope.request_fingerprint,
            }
        )
    )
    await bridge.aclose()
    copy.close()
    initial = f"after_replace:{cut.rsplit(':', 1)[1]}" if cut.startswith("recover:") else cut
    await child(factory, workspace_id, fixture, initial)
    before = snapshot(target)
    if cut.startswith("recover:"):
        await child(factory, workspace_id, fixture, cut)
        assert snapshot(target) == before
    plan = (
        ManagedPatchBatchCallPlan.model_validate_json(fixture.with_suffix(".plan").read_text())
        if cut != "plan_saved"
        else None
    )

    def forbidden(*args):
        pytest.fail("恢复不得新建、批准、执行或重新复核提案")

    monkeypatch.setattr(batch_agent_bridge, "prepare_patch_batch", forbidden)
    for method in ("save", "reply", "execute", "verify"):
        monkeypatch.setattr(ManagedPatchBatches, method, forbidden)
    with factory.open(workspace_id) as reopened:
        async with ManagedPatchBatchBridge(reopened) as recovered:
            for method in ("save", "reply", "execute", "_execute"):
                monkeypatch.setattr(reopened, method, forbidden)
            result = await recovered.recover(
                call, scope, CancelToken(), plan=plan, approval=approval(plan) if plan else None
            )
            if cut == "plan_saved":
                assert result.result.outcome == "unknown" and result.plan is None
            else:
                applied = sum((target / name).read_bytes() == b"after\r\n" for name in PATHS)
                assert result.result.outcome == ("succeeded" if applied == 3 else "failed")
                assert result.result.output["effect"] == (
                    "applied" if applied == 3 else "partial" if applied else "not_applied"
                )
                assert result.plan == plan
                no_run = cut in {"decision_mirrored", "run_before_commit"}
                assert (result.execution is None) == no_run
                if not no_run:
                    assert result.execution.run.stop_reason == (
                        "completed"
                        if cut in {"run_result_committed", "bridge_returned"}
                        else "interrupted"
                    )
                    assert all(
                        m.state == "pending" for m in result.execution.members[applied + 1 :]
                    )
            repeated = await recovered.recover(
                call, scope, CancelToken(), plan=plan, approval=approval(plan) if plan else None
            )
            assert repeated == result
    assert snapshot(target) == before
    assert snapshot(source.root) == original_source
