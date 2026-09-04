import asyncio
import subprocess
import sys

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.session.sqlite import SQLiteSessionStore
from tests.patches.test_kernel_patch import results
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case

SESSION_CUTS = [
    "runtime.after_tool_call",
    "runtime.after_patch_batch_plan",
    "runtime.before_approval_request",
    "runtime.after_approval_request",
    "runtime.before_approval_decision",
    "runtime.after_approval_decision",
    "runtime.after_approval_consumed",
    "runtime.before_tool",
    "runtime.after_tool",
    "runtime.after_tool_result",
    "runtime.before_terminal",
]
BATCH_CUTS = [
    "reservation_before_commit",
    "reservation_committed",
    "approval_before_commit",
    "approval_committed",
    "run_before_commit",
    "run_started",
    "preflight_complete",
    *(f"member_approved:{i}" for i in range(3)),
    *(f"member_completed:{i}" for i in range(3)),
    "run_result_before_commit",
    "run_result_committed",
]
FILE_CUTS = [
    f"{at}:{i}"
    for i in range(3)
    for at in (
        "started",
        "temp_created",
        "temp_synced",
        "temp_recorded",
        "before_replace",
        "after_replace",
        "directories_synced",
        "before_result",
        "result_recorded",
    )
]
RECOVERY_CUTS = [
    *(f"recover:member_reconciled:{i}" for i in range(3)),
    "recover:run_result_before_commit",
    "recover:run_result_committed",
    "recover:runtime.before_terminal",
]


async def crash(factory, workspace_id, database, cut):
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.patches.kernel_batch_crash_worker",
            str(factory.root),
            str(workspace_id),
            str(database),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 79, result.stderr


def forbidden(*args, **kwargs):
    pytest.fail("恢复不能准备、批准、执行或驱动模型")


@pytest.mark.parametrize("cut", [*SESSION_CUTS, *BATCH_CUTS, *FILE_CUTS, *RECOVERY_CUTS])
async def test_real_session_batch_boundaries_no_replay(group_case, tmp_path, monkeypatch, cut):
    source, factory, copy, _, _ = group_case
    workspace_id, target = copy.workspace_id, copy.workspace.root
    original = snapshot(source.root)
    copy.close()
    store = SQLiteSessionStore(tmp_path / "s.db")
    initial = (
        f"after_replace:{cut.rsplit(':', 1)[1]}"
        if cut.startswith("recover:member")
        else "after_replace:1"
        if cut.startswith("recover:")
        else cut
    )
    await crash(factory, workspace_id, store.path, initial)
    before = snapshot(target)
    if cut.startswith("recover:"):
        await crash(factory, workspace_id, store.path, cut)
        assert snapshot(target) == before
    for method in ("save", "reply", "execute", "verify"):
        monkeypatch.setattr(ManagedPatchBatches, method, forbidden)
    provider = ScriptedProvider([])
    monkeypatch.setattr(provider, "stream", forbidden)
    with factory.open(workspace_id) as reopened:
        for method in ("save", "reply", "execute", "_execute"):
            monkeypatch.setattr(reopened, method, forbidden)
        async with ManagedPatchBatchBridge(reopened) as bridge:
            monkeypatch.setattr(bridge, "prepare", forbidden)
            monkeypatch.setattr(bridge, "execute", forbidden)
            async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
                thread_id = (await store.thread_ids())[0]
                saved = await store.get_thread(thread_id)
                turn = saved.turns[-1]
                if cut in {
                    "runtime.after_approval_request",
                    "runtime.before_approval_decision",
                    "runtime.after_approval_decision",
                }:
                    assert turn.status == TurnStatus.WAITING_APPROVAL and not results(turn)
                    await runtime.cancel(thread_id, turn.turn_id)
                    saved = await store.get_thread(thread_id)
                    turn = saved.turns[-1]
                assert turn.status == TurnStatus.INTERRUPTED
                result = results(turn)[0]
                unverified = cut in {
                    "runtime.after_tool_call",
                    "runtime.after_patch_batch_plan",
                    "runtime.before_approval_request",
                    "runtime.after_approval_request",
                    "runtime.before_approval_decision",
                    "runtime.after_approval_decision",
                    "runtime.after_approval_consumed",
                    "runtime.before_tool",
                    "reservation_before_commit",
                    "reservation_committed",
                    "approval_before_commit",
                }
                if unverified:
                    assert result.outcome == "unknown" and result.patch_batch is None
                else:
                    applied = sum((target / p).read_bytes() == b"after\r\n" for p in PATHS)
                    assert result.outcome == ("succeeded" if applied == 3 else "failed")
                    assert result.patch_batch is not None
                    execution = result.patch_batch.execution
                    assert (execution is None) == (
                        cut in {"approval_committed", "run_before_commit"}
                    )
                    if execution is not None:
                        assert execution.effect == (
                            "applied" if applied == 3 else "partial" if applied else "not_applied"
                        )
                        assert result.patch_batch.origin == (
                            "execution"
                            if cut in {"runtime.after_tool_result", "runtime.before_terminal"}
                            else "recovery"
                        )
                        assert execution.run.stop_reason == (
                            "completed"
                            if cut
                            in {
                                "run_result_committed",
                                "runtime.after_tool",
                                "runtime.after_tool_result",
                                "runtime.before_terminal",
                            }
                            else "interrupted"
                        )
                assert replay(await store.events(thread_id)) == saved
            async with AgentRuntime(store, provider, patch_batches=bridge):
                assert await store.get_thread(thread_id) == saved
    assert snapshot(target) == before and snapshot(source.root) == original


@pytest.mark.parametrize(
    "config",
    [
        "missing_port",
        "contract_changed",
        "missing_plan",
        "damaged_approval",
        "missing_start",
        "same_bytes_new_inode",
        "missing_file",
        "diverged",
    ],
)
async def test_recovery_missing_or_uncertain_facts_never_replays(
    group_case, tmp_path, monkeypatch, config
):
    _, factory, copy, _, _ = group_case
    workspace_id, root = copy.workspace_id, copy.workspace.root
    copy.close()
    store = SQLiteSessionStore(tmp_path / "s.db")
    await crash(factory, workspace_id, store.path, "after_replace:1")
    target = root / PATHS[1]
    if config == "same_bytes_new_inode":
        temporary = root / "changed"
        temporary.write_bytes(target.read_bytes())
        temporary.replace(target)
    elif config == "missing_file":
        target.unlink()
    elif config == "diverged":
        target.write_text("external")
    before = tuple(
        (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in (root / x for x in PATHS)
        if p.exists()
    )
    with factory.open(workspace_id) as reopened:
        if config in {"missing_plan", "damaged_approval", "missing_start"}:
            with reopened._db:
                if config == "missing_plan":
                    reopened._db.execute("UPDATE batches SET request_id='gone'")
                elif config == "damaged_approval":
                    reopened._db.execute("UPDATE batch_approvals SET checksum='wrong'")
                else:
                    reopened._db.execute("DELETE FROM batch_run_events WHERE phase='started'")
        async with ManagedPatchBatchBridge(reopened) as bridge:
            if config == "contract_changed":
                definition = bridge.definition().model_copy(update={"version": "changed"})
                monkeypatch.setattr(bridge, "definition", lambda: definition)
            monkeypatch.setattr(bridge, "execute", forbidden)
            monkeypatch.setattr(bridge, "prepare", forbidden)
            provider = ScriptedProvider([])
            monkeypatch.setattr(provider, "stream", forbidden)
            async with AgentRuntime(
                store, provider, patch_batches=None if config == "missing_port" else bridge
            ):
                thread_id = (await store.thread_ids())[0]
                saved = await store.get_thread(thread_id)
                assert saved.turns[-1].status == TurnStatus.INTERRUPTED
                assert results(saved.turns[-1])[0].outcome == "unknown"
                assert replay(await store.events(thread_id)) == saved
    after = tuple(
        (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in (root / x for x in PATHS)
        if p.exists()
    )
    assert before == after
