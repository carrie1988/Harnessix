import asyncio
import sqlite3
import subprocess
import sys
from uuid import UUID

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.batch_diff import SQLiteBatchDiffPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from tests.patches.test_kernel_patch import results
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot


@pytest.mark.parametrize("view", ["plan", "effect"])
@pytest.mark.parametrize("point", ["after_insert", "before_commit", "after_commit"])
async def test_real_exit_atomic_report_and_facts_no_write_replay(
    group_case, tmp_path, monkeypatch, view, point
):
    source, factory, copy, _, _ = group_case
    original = snapshot(source.root)
    target, workspace_id = copy.workspace.root, copy.workspace_id
    copy.close()
    store = SQLiteSessionStore(tmp_path / "s.db")
    child = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.artifacts.batch_diff_crash_worker",
            str(factory.root),
            str(workspace_id),
            str(store.path),
            view,
            point,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 82, child.stderr
    before = snapshot(target)
    with sqlite3.connect(store.path) as db:
        count = db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]
        assert count == (int(view == "effect") + int(point == "after_commit"))
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def forbidden(*args, **kwargs):
        pytest.fail("恢复不得准备、执行或重放模型")

    artifacts = SQLiteArtifactStore(store)
    provider = ScriptedProvider([])
    monkeypatch.setattr(provider, "stream", forbidden)
    with factory.open(workspace_id) as reopened:
        async with ManagedPatchBatchBridge(reopened) as bridge:
            monkeypatch.setattr(bridge, "prepare", forbidden)
            monkeypatch.setattr(bridge, "execute", forbidden)
            async with AgentRuntime(
                store,
                provider,
                patch_batches=bridge,
                batch_diffs=SQLiteBatchDiffPublisher(artifacts, bridge),
            ) as runtime:
                thread_id = (await store.thread_ids())[0]
                thread = await store.get_thread(thread_id)
                turn = thread.turns[-1]
                if view == "plan" and point == "after_commit":
                    assert turn.status == TurnStatus.WAITING_APPROVAL
                    await runtime.cancel(thread_id, turn.turn_id)
                elif view == "effect":
                    result = results(turn)[0]
                    assert result.patch_batch.execution.effect == "applied"
                    assert result.diff_artifact is not None
                    assert result.patch_batch.origin == (
                        "execution" if point == "after_commit" else "recovery"
                    )
                assert snapshot(target) == before
            with sqlite3.connect(store.path) as db:
                for row in db.execute("SELECT artifact_id FROM agent_artifacts"):
                    assert (
                        await artifacts.read(thread_id, reopened.workspace.scope, UUID(row[0]))
                    ).text
            assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    assert snapshot(source.root) == original
