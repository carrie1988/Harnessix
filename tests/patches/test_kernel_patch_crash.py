import asyncio
import subprocess
import sys

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from tests.patches.test_agent_bridge import case as case
from tests.patches.test_kernel_patch import results

CUTS = [
    ("runtime.after_tool_call", None, False),
    ("runtime.after_patch_plan", "pending", False),
    ("runtime.before_approval_request", "pending", False),
    ("runtime.after_approval_request", "pending", True),
    ("runtime.before_approval_decision", "pending", True),
    ("runtime.after_approval_decision", "pending", True),
    ("runtime.after_approval_consumed", "pending", False),
    ("decision_mirrored", "approved", False),
    ("started", "observed_before", False),
    ("temp_created", "observed_before", False),
    ("temp_synced", "observed_before", False),
    ("temp_recorded", "observed_before", False),
    ("before_replace", "observed_before", False),
    ("after_replace", "observed_after", False),
    ("directories_synced", "observed_after", False),
    ("before_result", "observed_after", False),
    ("result_recorded", "applied", False),
    ("runtime.after_tool", "applied", False),
    ("runtime.after_tool_result", "applied", False),
    ("runtime.before_terminal", "applied", False),
]


async def crash(factory, workspace_id, database, cut):
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.patches.kernel_crash_worker",
            str(factory.root),
            str(workspace_id),
            str(database),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 76, result.stderr


def forbidden(*args, **kwargs):
    pytest.fail("恢复不得准备、批准、执行或调用模型")


@pytest.mark.parametrize("cut,state,waiting", CUTS)
async def test_session_patch_real_exit_no_replay(case, tmp_path, monkeypatch, cut, state, waiting):
    source, factory, copy = case
    workspace_id, target = copy.workspace_id, copy.workspace.root / "main.py"
    copy.close()
    store = SQLiteSessionStore(tmp_path / "session.db")
    await crash(factory, workspace_id, store.path, cut)
    before = target.stat()
    provider = ScriptedProvider([])
    monkeypatch.setattr(provider, "stream", forbidden)
    with factory.open(workspace_id) as reopened:
        for method in ("save", "reply", "execute", "verify"):
            monkeypatch.setattr(reopened, method, forbidden)
        async with ManagedPatchBridge(reopened) as bridge:
            monkeypatch.setattr(bridge, "prepare", forbidden)
            monkeypatch.setattr(bridge, "execute", forbidden)
            async with AgentRuntime(store, provider, patches=bridge) as runtime:
                thread_id = (await store.thread_ids())[0]
                snapshot = await store.get_thread(thread_id)
                turn = snapshot.turns[-1]
                if waiting:
                    assert turn.status == TurnStatus.WAITING_APPROVAL and not results(turn)
                    await runtime.cancel(thread_id, turn.turn_id)
                    snapshot = await store.get_thread(thread_id)
                    turn = snapshot.turns[-1]
                assert turn.status == (TurnStatus.CANCELLED if waiting else TurnStatus.INTERRUPTED)
                result = results(turn)[0]
                assert result.outcome == (
                    "succeeded" if state in {"applied", "observed_after"} else "failed"
                )
                if state is None:
                    assert result.patch is None
                else:
                    assert result.patch.state == state
                    assert result.patch.origin == (
                        "execution"
                        if cut in {"runtime.after_tool_result", "runtime.before_terminal"}
                        else "recovery"
                    )
                assert replay(await store.events(thread_id)) == snapshot
            async with AgentRuntime(store, provider, patches=bridge):
                assert await store.get_thread(thread_id) == snapshot
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


@pytest.mark.parametrize("config", ["missing", "changed", "diverged"])
async def test_recovery_unavailable_or_uncertain_stops_model(case, tmp_path, monkeypatch, config):
    _, factory, copy = case
    workspace_id, target = copy.workspace_id, copy.workspace.root / "main.py"
    copy.close()
    store = SQLiteSessionStore(tmp_path / "s.db")
    await crash(factory, workspace_id, store.path, "after_replace")
    if config == "diverged":
        target.write_bytes(b"external")
    before = target.stat()
    with factory.open(workspace_id) as reopened:
        async with ManagedPatchBridge(reopened) as bridge:
            if config == "changed":
                definition = bridge.definition().model_copy(update={"version": "changed"})
                monkeypatch.setattr(bridge, "definition", lambda: definition)
            monkeypatch.setattr(bridge, "execute", forbidden)
            provider = ScriptedProvider([])
            monkeypatch.setattr(provider, "stream", forbidden)
            async with AgentRuntime(
                store, provider, patches=None if config == "missing" else bridge
            ):
                thread_id = (await store.thread_ids())[0]
                snapshot = await store.get_thread(thread_id)
                assert snapshot.turns[-1].status == TurnStatus.INTERRUPTED
                assert results(snapshot.turns[-1])[0].outcome == "unknown"
                assert replay(await store.events(thread_id)) == snapshot
    after = target.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
