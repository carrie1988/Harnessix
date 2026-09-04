"""整组预留/决定和迁移的真实退出；不计作尚未实现的多文件写恢复。"""

import json
import sqlite3
import subprocess
import sys

import pytest

from harnessix.agent.errors import KernelError
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.workspace import ReadOperation
from tests.patches.test_managed import APPROVE
from tests.patches.test_managed_batches import (
    counts,
    snapshot,
)
from tests.patches.test_managed_batches import (
    group_case as group_case,
)

WORKER = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches import managed_batches, ledger_migrations
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batches import prepare_patch_batch
from harnessix.patches.managed import PatchWorkspaces
from harnessix.tools.workspace import ReadOperation
root, workspace_id, proposal_path, mode, cut = sys.argv[1:]
def fault(point):
    if point == cut:
        os._exit(76)
managed_batches._fault = fault
ledger_migrations._fault = fault
with PatchWorkspaces(Path(root)).open(UUID(workspace_id)) as copy:
    groups = managed_batches.ManagedPatchBatches(copy)
    if mode == 'save':
        proposal = PatchBatchProposal.model_validate_json(Path(proposal_path).read_text())
        prepared = prepare_patch_batch(copy.workspace, proposal, ReadOperation())
        groups.save(prepared, 'stable', ReadOperation())
    elif mode == 'reply':
        record = groups.lookup('stable', ReadOperation())
        groups.reply(record.plan.batch_id, record.plan.approval_fingerprint,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor='本地验收'), ReadOperation())
raise AssertionError('没有到达退出切点')
"""


def run_worker(factory, workspace_id, proposal, mode, cut):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            WORKER,
            str(factory.root),
            str(workspace_id),
            str(proposal),
            mode,
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 76, result.stderr


@pytest.mark.parametrize(
    "cut",
    [
        "batch_reserved",
        *(f"member_reserved:{i}" for i in range(3)),
        "reservation_before_commit",
        "reservation_committed",
        "approval_before_commit",
        "approval_committed",
    ],
)
def test_real_exit_group_commit_boundaries(group_case, tmp_path, cut):
    source, factory, copy, groups, batch = group_case
    proposal = tmp_path / "proposal.json"
    proposal.write_text(batch.proposal.model_dump_json())
    mode = "reply" if cut.startswith("approval_") else "save"
    if mode == "reply":
        groups.save(batch, "stable", ReadOperation())
    original, private = snapshot(source.root), snapshot(copy.workspace.root)
    root, workspace_id = copy.workspace.root, copy.workspace_id
    copy.close()
    run_worker(factory, workspace_id, proposal, mode, cut)
    before_reopen = snapshot(root)
    with factory.open(workspace_id) as reopened:
        groups = ManagedPatchBatches(reopened)
        result = groups.lookup("stable", ReadOperation())
        committed = mode == "reply" or cut == "reservation_committed"
        assert (result is not None) == committed
        if result is not None:
            assert result.decision == (APPROVE if cut == "approval_committed" else None)
            assert groups.get(result.plan.batch_id, ReadOperation()) == result
            for member in result.plan.members:
                with pytest.raises(KernelError) as error:
                    reopened.execute(member.plan_id, member.approval_fingerprint, ReadOperation())
                assert error.value.code == "patch_batch_member_requires_group"
        assert counts(reopened) == (
            3 if committed else 0,
            3 if committed else 0,
            int(committed),
            int(cut == "approval_committed"),
        )
    assert before_reopen == snapshot(root) == private
    assert snapshot(source.root) == original


def legacy_fixture(copy, batch):
    """单元测试恢复 v1 表形；实际旧 wheel 验收另由升级探针完成。"""
    expected = []
    for index, patch in enumerate(batch.patches):
        record = copy.save(patch, f"legacy-{index}", ReadOperation())
        if index:
            record = copy.reply(record.plan_id, record.approval_fingerprint, APPROVE)
        if index == 2:
            record = copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
        expected.append(record)
    copy._db.execute("DROP INDEX plans_owner_batch")
    copy._db.execute("ALTER TABLE plans DROP COLUMN owner_batch_id")
    copy._db.execute("DROP TABLE batch_approvals")
    copy._db.execute("DROP TABLE batches")
    copy._db.execute("PRAGMA user_version=1")
    return expected


def ledger_bytes(db):
    return json.dumps(
        [
            db.execute("SELECT * FROM metadata").fetchall(),
            db.execute("SELECT path,hex(body) FROM baseline ORDER BY path").fetchall(),
            db.execute(
                "SELECT id,request_id,proposal,hex(before_image),hex(after_image) "
                "FROM plans ORDER BY id"
            ).fetchall(),
            db.execute("SELECT * FROM events ORDER BY sequence").fetchall(),
        ]
    )


@pytest.mark.parametrize(
    "cut", ["migration_before_version", "migration_before_commit", "migration_committed"]
)
def test_real_exit_migration_is_atomic_and_preserves_old_events(group_case, tmp_path, cut):
    source, factory, copy, _, batch = group_case
    expected = legacy_fixture(copy, batch)
    before = ledger_bytes(copy._db)
    original, private = snapshot(source.root), snapshot(copy.workspace.root)
    root, workspace_id, db_path = (
        copy.workspace.root,
        copy.workspace_id,
        copy._bundle.root / "ledger.sqlite",
    )
    inode = db_path.stat().st_ino
    copy.close()
    run_worker(factory, workspace_id, tmp_path / "unused", "migrate", cut)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (
            2 if cut == "migration_committed" else 1
        )
        assert ledger_bytes(db) == before
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert ("batches" in tables) == (cut == "migration_committed")
    with factory.open(workspace_id) as reopened:
        assert reopened._db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert ledger_bytes(reopened._db) == before
        for record in expected:
            assert reopened.get(record.plan_id) == record
        assert counts(reopened)[2:] == (0, 0)
    assert db_path.stat().st_ino == inode
    assert snapshot(root) == private and snapshot(source.root) == original


@pytest.mark.parametrize("kind", ["event", "baseline", "application", "future", "ddl_collision"])
def test_invalid_legacy_ledger_not_silently_upgraded(group_case, kind):
    _, factory, copy, _, batch = group_case
    legacy_fixture(copy, batch)
    if kind == "event":
        copy._db.execute("UPDATE events SET checksum='bad'")
    elif kind == "baseline":
        copy._db.execute("UPDATE baseline SET body=?", (b"bad",))
    elif kind == "application":
        copy._db.execute("PRAGMA application_id=0")
    elif kind == "future":
        copy._db.execute("PRAGMA user_version=99")
    else:
        copy._db.execute("CREATE TABLE batches (unrelated TEXT)")
    path = copy._bundle.root / "ledger.sqlite"
    copy.close()
    with pytest.raises(KernelError):
        factory.open(copy.workspace_id)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (99 if kind == "future" else 1)
        columns = {row[1] for row in db.execute("PRAGMA table_info(plans)")}
        assert "owner_batch_id" not in columns
