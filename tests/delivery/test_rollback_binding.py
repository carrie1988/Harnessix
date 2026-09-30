"""Rollback必须属于原Workspace；目录身份漂移不能产生新的批准计划。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import planner
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore


@pytest.fixture
def published_change(
    tmp_path: Path,
) -> Iterator[
    tuple[
        Path,
        SQLiteWorkspaceTransactionStore,
        WorkspaceLeaseStore,
        WorkspaceTransactionRuntime,
        WorkspaceTransactionRecord,
    ]
]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "modify.txt").write_bytes(b"before")
    with SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions") as store:
        with WorkspaceLeaseStore(tmp_path / "state/leases.db") as leases:
            runtime = WorkspaceTransactionRuntime(store, leases)
            original = store.save(
                prepare_workspace_transaction(
                    root,
                    {"modify.txt": DesiredWorkspaceFile(b"after", 0o644)},
                    request_id="original",
                )
            )
            lease = leases.acquire(
                original.plan.source.workspace_id, "rollback-test", ttl_seconds=60
            )
            published = runtime.publish(
                original.transaction_id,
                root,
                approval_fingerprint=original.plan.fingerprint,
                lease=lease,
            )
            yield root, store, leases, runtime, published


@pytest.mark.parametrize("root_change", ["different", "relocated", "replaced"])
def test_rollback_rejects_a_root_outside_original_workspace(
    tmp_path: Path,
    published_change,
    root_change: str,
) -> None:
    root, store, _leases, runtime, original = published_change
    other = tmp_path / "other-workspace"
    if root_change == "different":
        other.mkdir()
        (other / "modify.txt").write_bytes(b"after")
    else:
        root.rename(other)
        if root_change == "replaced":
            root.mkdir()
            (root / "modify.txt").write_bytes(b"after")
            other = root
    with pytest.raises(KernelError) as error:
        runtime.build_rollback(original.transaction_id, other, request_id="wrong-root")
    assert error.value.code == "delivery_source_changed"
    assert store.lookup("wrong-root") is None
    assert store.load(original.transaction_id) == original
    assert (other / "modify.txt").read_bytes() == b"after"


def test_rollback_rejects_wrong_root_before_reading_original_blobs(
    tmp_path: Path,
    published_change,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, store, _leases, runtime, original = published_change
    other = tmp_path / "other-workspace"
    other.mkdir()
    (other / "modify.txt").write_bytes(b"after")

    def forbidden_blob(_digest: str) -> bytes:
        raise AssertionError("Workspace身份校验前不能读取原事务正文")

    monkeypatch.setattr(store, "blob", forbidden_blob)
    with pytest.raises(KernelError) as error:
        runtime.build_rollback(original.transaction_id, other, request_id="before-blob")
    assert error.value.code == "delivery_source_changed"
    assert store.lookup("before-blob") is None


def test_rollback_rechecks_planner_source_after_root_replacement(
    tmp_path: Path,
    published_change,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _leases, runtime, original = published_change
    previous = tmp_path / "previous-workspace"
    prepare = planner.prepare_workspace_transaction

    def replaced_source(supplied_root, desired, **kwargs):
        # 第一轮根校验后置换目录，Planner本身仍按正常路径捕获完整新来源。
        root.rename(previous)
        root.mkdir()
        (root / "modify.txt").write_bytes(b"after")
        return prepare(supplied_root, desired, **kwargs)

    monkeypatch.setattr(planner, "prepare_workspace_transaction", replaced_source)
    with pytest.raises(KernelError) as error:
        runtime.build_rollback(original.transaction_id, root, request_id="root-race")
    assert error.value.code == "delivery_source_changed"
    assert store.lookup("root-race") is None
    assert store.load(original.transaction_id) == original
    assert (root / "modify.txt").read_bytes() == b"after"
    assert (previous / "modify.txt").read_bytes() == b"after"


def test_rollback_keeps_original_root_and_unrelated_user_changes(published_change) -> None:
    root, store, leases, runtime, original = published_change
    unrelated = root / "user-note.txt"
    unrelated.write_bytes(b"unrelated user edit")
    rollback = runtime.build_rollback(original.transaction_id, root / ".", request_id="rollback")
    assert rollback.plan.source.workspace_id == original.plan.source.workspace_id
    assert rollback.plan.source.revision != original.plan.source.revision
    assert rollback.plan.fingerprint != original.plan.fingerprint
    lease = leases.acquire(original.plan.source.workspace_id, "rollback-test", ttl_seconds=60)
    with pytest.raises(KernelError) as error:
        runtime.publish(
            rollback.transaction_id,
            root,
            approval_fingerprint=original.plan.fingerprint,
            lease=lease,
        )
    assert error.value.code == "delivery_approval_mismatch"
    assert store.load(rollback.transaction_id).state == "prepared"
    assert (root / "modify.txt").read_bytes() == b"after"
    published = runtime.publish(
        rollback.transaction_id,
        root,
        approval_fingerprint=rollback.plan.fingerprint,
        lease=lease,
    )
    assert published.state == "published"
    assert store.load(original.transaction_id) == original
    assert (root / "modify.txt").read_bytes() == b"before"
    assert unrelated.read_bytes() == b"unrelated user edit"
