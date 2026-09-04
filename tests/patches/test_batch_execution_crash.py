"""真实退出矩阵：组消费、每成员替换/结果、观察中断及 v2→v3。"""

import sqlite3
import subprocess
import sys

import pytest

from harnessix.agent.errors import KernelError
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.workspace import ReadOperation
from tests.patches.test_batch_crash import ledger_bytes
from tests.patches.test_batch_execution import AFTER, BEFORE, approved, no_write
from tests.patches.test_managed import failure
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot

WORKER = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.patches import managed, batch_execution, batch_run_migrations
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.workspace import ReadOperation
root, workspace_id, batch_id, fingerprint, mode, cut = sys.argv[1:]
current = -1
def group_fault(point):
    if point == cut:
        os._exit(77)
def file_fault(point):
    global current
    if point == 'started':
        current += 1
    group_fault(f'file:{current}:{point}')
def no_write(*args, **kwargs):
    raise AssertionError('恢复不能执行补丁')
managed._fault = file_fault
batch_execution._fault = group_fault
batch_run_migrations._fault = group_fault
with managed.PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
    groups = ManagedPatchBatches(copy)
    if mode == 'execute':
        groups.execute(UUID(batch_id), fingerprint, ReadOperation())
    elif mode == 'reconcile':
        copy._execute = no_write
        groups.save = groups.reply = groups.execute = no_write
        groups.reconcile(UUID(batch_id), ReadOperation())
raise AssertionError('未命中退出切点')
"""


def exit_at(factory, workspace_id, plan, mode, cut):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            WORKER,
            str(factory.root),
            str(workspace_id),
            str(plan.batch_id),
            plan.approval_fingerprint,
            mode,
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 77, result.stderr


CUTS = [
    ("run_before_commit", 0),
    ("run_started", 0),
    ("preflight_complete", 0),
    *((f"member_approved:{i}", i) for i in range(3)),
    *((f"member_completed:{i}", i + 1) for i in range(3)),
    *((f"file:{i}:{cut}", i) for i in range(3) for cut in BEFORE),
    *((f"file:{i}:{cut}", i + 1) for i in range(3) for cut in AFTER),
    ("run_result_before_commit", 3),
    ("run_result_committed", 3),
]


@pytest.mark.parametrize("cut,applied", CUTS)
def test_real_exit_preserves_prefix_and_recovery_never_replays(
    group_case, monkeypatch, cut, applied
):
    source, factory, copy, groups, batch = group_case
    plan = approved(groups, batch)
    workspace_id, root = copy.workspace_id, copy.workspace.root
    original, initial = snapshot(source.root), snapshot(root)
    copy.close()
    exit_at(factory, workspace_id, plan, "execute", cut)
    before_recovery = snapshot(root)
    for index, entry in enumerate(before_recovery):
        assert entry[0] == (b"after\r\n" if index < applied else b"before\r\n")
    assert before_recovery[applied:] == initial[applied:]
    with factory.open(workspace_id) as recovered:
        groups = ManagedPatchBatches(recovered)
        pending_result = groups.get_execution(plan.batch_id, ReadOperation())
        if cut != "run_before_commit":
            assert pending_result.run.phase == (
                "finished" if cut == "run_result_committed" else "started"
            )
        monkeypatch.setattr(recovered, "_execute", no_write)
        monkeypatch.setattr(groups, "save", no_write)
        monkeypatch.setattr(groups, "reply", no_write)
        result = groups.reconcile(plan.batch_id, ReadOperation())
        if cut == "run_before_commit":
            assert result is None
        else:
            assert result.effect == (
                "applied" if applied == 3 else "partial" if applied else "not_applied"
            )
            assert result.run.stop_reason == (
                "completed" if cut == "run_result_committed" else "interrupted"
            )
            assert groups.reconcile(plan.batch_id, ReadOperation()) == result
            with failure("patch_not_executable"):
                groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
        assert snapshot(root) == before_recovery
    assert snapshot(source.root) == original


@pytest.mark.parametrize(
    "cut", ["member_reconciled:1", "run_result_before_commit", "run_result_committed"]
)
def test_real_exit_while_reconciling_keeps_observation_not_execution(group_case, cut):
    source, factory, copy, groups, batch = group_case
    plan = approved(groups, batch)
    workspace_id, root = copy.workspace_id, copy.workspace.root
    original = snapshot(source.root)
    copy.close()
    exit_at(factory, workspace_id, plan, "execute", "file:1:after_replace")
    before = snapshot(root)
    exit_at(factory, workspace_id, plan, "reconcile", cut)
    with factory.open(workspace_id) as recovered:
        result = ManagedPatchBatches(recovered).reconcile(plan.batch_id, ReadOperation())
        assert result.effect == "partial" and result.run.stop_reason == "interrupted"
        assert [member.state for member in result.members] == [
            "applied",
            "observed_after",
            "pending",
        ]
    assert snapshot(root) == before and snapshot(source.root) == original


@pytest.mark.parametrize("cut", ["runs_before_version", "runs_before_commit", "runs_committed"])
def test_real_exit_v2_to_v3_migration_is_atomic(group_case, cut):
    source, factory, copy, groups, batch = group_case
    plan = approved(groups, batch)
    # 单元表形夹具；真实 f0adddc wheel 升级证据由独立探针补齐。
    copy._db.execute("DROP TABLE batch_run_events")
    copy._db.execute("PRAGMA user_version=2")
    baseline = ledger_bytes(copy._db)
    group_rows = copy._db.execute("SELECT * FROM batches").fetchall()
    approvals = copy._db.execute("SELECT * FROM batch_approvals").fetchall()
    path = copy._bundle.root / "ledger.sqlite"
    original, before = snapshot(source.root), snapshot(copy.workspace.root)
    inode, workspace_id = path.stat().st_ino, copy.workspace_id
    copy.close()
    exit_at(factory, workspace_id, plan, "migrate", cut)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (
            3 if cut == "runs_committed" else 2
        )
        assert ledger_bytes(db) == baseline
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert ("batch_run_events" in tables) == (cut == "runs_committed")
    with factory.open(workspace_id) as recovered:
        assert recovered._db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert ledger_bytes(recovered._db) == baseline
        assert recovered._db.execute("SELECT * FROM batches").fetchall() == group_rows
        assert recovered._db.execute("SELECT * FROM batch_approvals").fetchall() == approvals
        assert ManagedPatchBatches(recovered).get_execution(plan.batch_id, ReadOperation()) is None
        assert snapshot(recovered.workspace.root) == before
    assert path.stat().st_ino == inode and snapshot(source.root) == original


@pytest.mark.parametrize("kind", ["group", "member", "orphan", "group_id", "ddl_collision"])
def test_invalid_v2_groups_do_not_advance_version(group_case, kind):
    _, factory, copy, groups, batch = group_case
    approved(groups, batch)
    copy._db.execute("DROP TABLE batch_run_events")
    copy._db.execute("PRAGMA user_version=2")
    if kind == "group":
        copy._db.execute("UPDATE batches SET checksum='invalid'")
    elif kind == "member":
        copy._db.execute("UPDATE events SET checksum='invalid'")
    elif kind == "orphan":
        copy._db.execute("PRAGMA foreign_keys=OFF")
        copy._db.execute("UPDATE plans SET owner_batch_id='missing'")
    elif kind == "group_id":
        copy._db.execute("PRAGMA foreign_keys=OFF")
        copy._db.execute("UPDATE batches SET id='invalid'")
    else:
        copy._db.execute("CREATE TABLE batch_run_events(unrelated TEXT)")
    path = copy._bundle.root / "ledger.sqlite"
    copy.close()
    with pytest.raises(KernelError):
        factory.open(copy.workspace_id)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
