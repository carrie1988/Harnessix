from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import filesystem
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="普通Workspace发布使用POSIX端口")

_CRASH_WORKER = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.delivery import filesystem
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore
transaction_root, lease_path, workspace, transaction_id = sys.argv[1:]
def crash(point):
    if point == 'effect_applied:0':
        os._exit(87)
filesystem._fault = crash
with SQLiteWorkspaceTransactionStore(Path(transaction_root)) as store:
    with WorkspaceLeaseStore(Path(lease_path)) as leases:
        runtime = WorkspaceTransactionRuntime(store, leases)
        record = store.load(UUID(transaction_id))
        lease = leases.acquire(record.plan.source.workspace_id, 'crash-worker', ttl_seconds=60)
        runtime.publish(record.transaction_id, Path(workspace),
            approval_fingerprint=record.plan.fingerprint, lease=lease)
raise AssertionError('未到达硬退出切点')
"""


def _runtime(tmp_path: Path):
    store = SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions")
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db")
    return store, leases, WorkspaceTransactionRuntime(store, leases)


def test_publish_create_modify_delete_and_idempotent_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src/modify.py").write_bytes(b"before\n")
    (workspace / "delete.txt").write_bytes(b"delete\n")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "src/modify.py": DesiredWorkspaceFile(b"after\n", 0o755),
            "src/create.py": DesiredWorkspaceFile(b"created\n", 0o644),
            "delete.txt": DesiredWorkspaceFile(None),
        },
        request_id="publish",
    )
    store, leases, runtime = _runtime(tmp_path)
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)
        published = runtime.publish(
            record.transaction_id,
            workspace,
            approval_fingerprint=record.plan.fingerprint,
            lease=lease,
        )
        repeated = runtime.publish(
            record.transaction_id,
            workspace,
            approval_fingerprint=record.plan.fingerprint,
            lease=lease,
        )
    finally:
        leases.close()
        store.close()
    assert published == repeated and published.state == "published"
    assert (workspace / "src/modify.py").read_bytes() == b"after\n"
    assert (workspace / "src/modify.py").stat().st_mode & 0o777 == 0o755
    assert (workspace / "src/create.py").read_bytes() == b"created\n"
    assert not (workspace / "delete.txt").exists()


def test_source_drift_before_first_effect_preserves_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    prepared = prepare_workspace_transaction(
        workspace,
        {"target.txt": DesiredWorkspaceFile(b"after", 0o644)},
        request_id="drift",
    )
    store, leases, runtime = _runtime(tmp_path)
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)
        target.write_bytes(b"user-change")
        with pytest.raises(KernelError) as changed:
            runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        assert store.load(record.transaction_id).state == "prepared"
    finally:
        leases.close()
        store.close()
    assert changed.value.code == "execution_plan_stale"
    assert target.read_bytes() == b"user-change"


def test_reconcile_effect_after_crash_and_resume_remaining_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"a-before")
    (workspace / "b.txt").write_bytes(b"b-before")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "a.txt": DesiredWorkspaceFile(b"a-after", 0o644),
            "b.txt": DesiredWorkspaceFile(b"b-after", 0o644),
        },
        request_id="resume",
    )
    store, leases, runtime = _runtime(tmp_path)
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)

        def crash(point: str) -> None:
            if point == "effect_applied:0":
                raise RuntimeError("crash")

        monkeypatch.setattr(filesystem, "_fault", crash)
        with pytest.raises(RuntimeError):
            runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        assert store.load(record.transaction_id).state == "publishing"
        assert (workspace / "a.txt").read_bytes() == b"a-after"
        assert (workspace / "b.txt").read_bytes() == b"b-before"
        interrupted = runtime.reconcile(record.transaction_id, workspace)
        assert interrupted.state == "interrupted" and interrupted.cursor == 1
        monkeypatch.setattr(filesystem, "_fault", lambda _: None)
        published = runtime.publish(
            record.transaction_id,
            workspace,
            approval_fingerprint=record.plan.fingerprint,
            lease=lease,
        )
    finally:
        leases.close()
        store.close()
    assert published.state == "published"
    assert (workspace / "b.txt").read_bytes() == b"b-after"


@pytest.mark.parametrize("divergence", ["third-content", "out-of-order"])
def test_reconcile_rejects_unowned_or_out_of_order_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, divergence: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_bytes(b"a-before")
    second.write_bytes(b"b-before")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "a.txt": DesiredWorkspaceFile(b"a-after", 0o644),
            "b.txt": DesiredWorkspaceFile(b"b-after", 0o644),
        },
        request_id=f"diverged-{divergence}",
    )
    store, leases, runtime = _runtime(tmp_path)
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)

        def crash(point: str) -> None:
            if point == "effect_applied:0":
                raise RuntimeError("crash")

        monkeypatch.setattr(filesystem, "_fault", crash)
        with pytest.raises(RuntimeError):
            runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        if divergence == "third-content":
            second.write_bytes(b"foreign")
        else:
            first.write_bytes(b"a-before")
            second.write_bytes(b"b-after")
        reconciled = runtime.reconcile(record.transaction_id, workspace)
    finally:
        leases.close()
        store.close()
    assert reconciled.state == "diverged"
    assert reconciled.error_code in {"delivery_source_changed", "delivery_order_diverged"}


def test_expired_lease_stops_before_next_member_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_bytes(b"a-before")
    second.write_bytes(b"b-before")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "a.txt": DesiredWorkspaceFile(b"a-after", 0o644),
            "b.txt": DesiredWorkspaceFile(b"b-after", 0o644),
        },
        request_id="lease-expires",
    )
    now = [100.0]
    store = SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions")
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db", clock=lambda: now[0])
    runtime = WorkspaceTransactionRuntime(store, leases)
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "first-owner", ttl_seconds=1)

        def expire(point: str) -> None:
            if point == "member_recorded:0":
                now[0] = 102.0

        monkeypatch.setattr(filesystem, "_fault", expire)
        with pytest.raises(KernelError) as lost:
            runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        stopped = store.load(record.transaction_id)
        assert stopped.state == "publishing" and stopped.cursor == 1
        assert first.read_bytes() == b"a-after"
        assert second.read_bytes() == b"b-before"

        replacement = leases.acquire(
            record.plan.source.workspace_id, "second-owner", ttl_seconds=60
        )
        monkeypatch.setattr(filesystem, "_fault", lambda _: None)
        published = runtime.publish(
            record.transaction_id,
            workspace,
            approval_fingerprint=record.plan.fingerprint,
            lease=replacement,
        )
    finally:
        leases.close()
        store.close()
    assert lost.value.code == "workspace_lease_lost"
    assert published.state == "published"
    assert second.read_bytes() == b"b-after"


def test_rollback_is_a_new_approved_transaction_and_preserves_original(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "modify.txt").write_bytes(b"before")
    (workspace / "delete.txt").write_bytes(b"restore")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "modify.txt": DesiredWorkspaceFile(b"after", 0o644),
            "delete.txt": DesiredWorkspaceFile(None),
            "created.txt": DesiredWorkspaceFile(b"created", 0o644),
        },
        request_id="original",
    )
    store, leases, runtime = _runtime(tmp_path)
    try:
        original = store.save(prepared)
        lease = leases.acquire(original.plan.source.workspace_id, "test", ttl_seconds=60)
        runtime.publish(
            original.transaction_id,
            workspace,
            approval_fingerprint=original.plan.fingerprint,
            lease=lease,
        )
        rollback = runtime.build_rollback(original.transaction_id, workspace, request_id="rollback")
        restored = runtime.publish(
            rollback.transaction_id,
            workspace,
            approval_fingerprint=rollback.plan.fingerprint,
            lease=lease,
        )
        original_after = store.load(original.transaction_id)
    finally:
        leases.close()
        store.close()
    assert restored.state == "published"
    assert original_after.state == "published"
    assert (workspace / "modify.txt").read_bytes() == b"before"
    assert (workspace / "delete.txt").read_bytes() == b"restore"
    assert not (workspace / "created.txt").exists()


def test_approval_and_fencing_mismatch_fail_before_effect(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    prepared = prepare_workspace_transaction(
        workspace,
        {"target.txt": DesiredWorkspaceFile(b"after", 0o644)},
        request_id="fence",
    )
    store, leases, runtime = _runtime(tmp_path)
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "owner", ttl_seconds=60)
        with pytest.raises(KernelError) as approval:
            runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint="f" * 64,
                lease=lease,
            )
        wrong = lease.model_copy(update={"workspace_id": "f" * 64})
        with pytest.raises(KernelError) as fencing:
            runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint=record.plan.fingerprint,
                lease=wrong,
            )
    finally:
        leases.close()
        store.close()
    assert approval.value.code == "delivery_approval_mismatch"
    assert fencing.value.code == "workspace_lease_lost"
    assert target.read_bytes() == b"before"


def test_real_process_exit_after_replace_reconciles_without_repeating_effect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"a-before")
    (workspace / "b.txt").write_bytes(b"b-before")
    prepared = prepare_workspace_transaction(
        workspace,
        {
            "a.txt": DesiredWorkspaceFile(b"a-after", 0o644),
            "b.txt": DesiredWorkspaceFile(b"b-after", 0o644),
        },
        request_id="hard-exit",
    )
    transaction_root = tmp_path / "state/transactions"
    lease_path = tmp_path / "state/leases.db"
    with SQLiteWorkspaceTransactionStore(transaction_root) as store:
        record = store.save(prepared)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_WORKER,
            str(transaction_root),
            str(lease_path),
            str(workspace),
            str(record.transaction_id),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 87, completed.stderr
    with SQLiteWorkspaceTransactionStore(transaction_root) as store:
        with WorkspaceLeaseStore(lease_path) as leases:
            runtime = WorkspaceTransactionRuntime(store, leases)
            interrupted = runtime.reconcile(record.transaction_id, workspace)
            assert interrupted.state == "interrupted" and interrupted.cursor == 1
            lease = leases.acquire(record.plan.source.workspace_id, "crash-worker", ttl_seconds=60)
            published = runtime.publish(
                record.transaction_id,
                workspace,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
    assert published.state == "published"
    assert (workspace / "a.txt").read_bytes() == b"a-after"
    assert (workspace / "b.txt").read_bytes() == b"b-after"
