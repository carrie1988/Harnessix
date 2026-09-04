import asyncio
import json
import subprocess
import sys

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.patches import agent_bridge
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from tests.patches.bridge_helpers import approval, make_call
from tests.patches.test_agent_bridge import case as case


@pytest.mark.parametrize(
    "cut,state",
    [
        ("plan_saved", "pending"),
        ("decision_mirrored", "approved"),
        ("started", "observed_before"),
        ("temp_created", "observed_before"),
        ("temp_synced", "observed_before"),
        ("temp_recorded", "observed_before"),
        ("before_replace", "observed_before"),
        ("after_replace", "observed_after"),
        ("directories_synced", "observed_after"),
        ("before_result", "observed_after"),
        ("result_recorded", "applied"),
        ("bridge_returned", "applied"),
    ],
)
async def test_real_exit_recovers_same_call_without_preparing_or_writing(
    case, tmp_path, monkeypatch, cut, state
):
    source, factory, copy = case
    bridge = ManagedPatchBridge(copy)
    call, scope = make_call(copy, bridge)
    workspace_id = copy.workspace_id
    target = copy.workspace.root / "main.py"
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
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.patches.bridge_crash_worker",
            str(factory.root),
            str(workspace_id),
            str(fixture),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 75, result.stderr
    before = target.stat()
    plan_path = fixture.with_suffix(".plan")
    plan = (
        ManagedPatchCallPlan.model_validate_json(plan_path.read_text())
        if plan_path.exists()
        else None
    )
    assert (plan is None) == (cut == "plan_saved")

    def forbidden(*args):
        pytest.fail("恢复不允许准备、批准或执行")

    monkeypatch.setattr(agent_bridge, "prepare_patch", forbidden)
    with factory.open(workspace_id) as reopened:
        for method in ("save", "reply", "execute", "verify"):
            monkeypatch.setattr(reopened, method, forbidden)
        async with ManagedPatchBridge(reopened) as recovered:
            result = await recovered.recover(
                call, scope, CancelToken(), plan=plan, approval=approval(plan) if plan else None
            )
            assert result.record.state == state
            assert result.plan.request_id == recovered._request(scope)
            assert result.result.outcome == (
                "succeeded" if state in {"applied", "observed_after"} else "failed"
            )
            assert (
                await recovered.recover(
                    call, scope, CancelToken(), plan=result.plan, approval=approval(result.plan)
                )
                == result
            )
    after = target.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    assert target.read_bytes() == (
        b"after\r\n" if state in {"applied", "observed_after"} else b"before\r\n"
    )
    assert (source.root / "main.py").read_bytes() == b"before\r\n"
