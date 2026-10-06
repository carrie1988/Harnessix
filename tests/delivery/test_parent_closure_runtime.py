"""完整父历史经过真实规划、账本、发布与独立逆向事务，不缩减为模型容量测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import filesystem, planner
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_v2_contracts import (
    WorkspaceTransactionPlanV2,
    WorkspaceTransactionRecordV2,
)
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure

pytestmark = pytest.mark.skipif(os.name != "posix", reason="本组验证真实POSIX事务端口")


def _checkpoint():
    pass


def _files(root: Path, count: int):
    root.mkdir()
    desired = {}
    for index in range(count):
        path = f"d{index:03}/nested/leaf.txt"
        target = root / path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"before\n")
        target.chmod(0o644)
        desired[path] = DesiredWorkspaceFile(b"after\n", 0o644)
    return desired


def _prepare(root, desired, store):
    return prepare_workspace_transaction_v2(
        root,
        desired,
        request_id="complete-history",
        checkpoint=_checkpoint,
        write_blob=store.put_blob,
        read_blob=store.blob,
    )


@pytest.mark.parametrize("count", [1, 128, 255])
def test_real_capacity_publish_independent_rollback_and_readonly_reopen(tmp_path, count):
    root, state = tmp_path / "workspace", tmp_path / "state"
    desired = _files(root, count)
    with (
        SQLiteWorkspaceTransactionStore(state / "transactions") as store,
        WorkspaceLeaseStore(state / "leases.db") as leases,
    ):
        prepared = _prepare(root, desired, store)
        assert type(prepared.plan) is WorkspaceTransactionPlanV2
        assert len(prepared.plan.source.resources) == count + 1
        assert (
            len(
                read_workspace_parent_closure(
                    prepared.plan.source, store.blob, checkpoint=_checkpoint
                )
            )
            == 2 * count + 1
        )
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=300)
        runtime = WorkspaceTransactionRuntime(store, leases)
        published = runtime.publish(
            record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
        )
        assert type(published) is WorkspaceTransactionRecordV2 and published.state == "published"
        assert all((root / path).read_bytes() == b"after\n" for path in desired)
        # 发布与独立批准的回滚各持有原300秒租约，不复用已消耗的旧窗口。
        leases.release(lease)
        inverse = runtime.build_rollback(
            record.transaction_id, root, request_id="independent-inverse"
        )
        lease = leases.acquire(inverse.plan.source.workspace_id, "inverse-test", ttl_seconds=300)
        assert type(inverse) is WorkspaceTransactionRecordV2 and inverse.state == "prepared"
        assert inverse.transaction_id != record.transaction_id
        with pytest.raises(KernelError, match="批准指纹"):
            runtime.publish(
                inverse.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        rolled_back = runtime.publish(
            inverse.transaction_id, root, approval_fingerprint=inverse.plan.fingerprint, lease=lease
        )
        assert rolled_back.state == "published" and store.load(record.transaction_id) == published
        assert all((root / path).read_bytes() == b"before\n" for path in desired)
        leases.release(lease)
    with SQLiteWorkspaceTransactionStore(state / "transactions", read_only=True) as reader:
        assert reader.load(record.transaction_id) == published
        assert reader.load(inverse.transaction_id) == rolled_back
        for (payload,) in reader._db.execute("SELECT payload FROM workspace_transaction_events"):
            decoded = reader.decode_payload(payload)
            assert type(decoded.record) is WorkspaceTransactionRecordV2
            assert len(decoded.references) >= 3


def test_original_v1_capacity_failure_is_not_redefined(tmp_path):
    root = tmp_path / "workspace"
    desired = _files(root, 128)
    with pytest.raises(KernelError) as failed:
        prepare_workspace_transaction(root, desired, request_id="legacy-negative")
    assert failed.value.code == "workspace_snapshot_limit"


@pytest.mark.parametrize("change", ["membership", "mode", "identity", "leaf"])
def test_any_historical_parent_change_rejected_before_effect(tmp_path, change):
    root, state = tmp_path / "workspace", tmp_path / "state"
    desired = _files(root, 1)
    with (
        SQLiteWorkspaceTransactionStore(state / "transactions") as store,
        WorkspaceLeaseStore(state / "leases.db") as leases,
    ):
        record = store.save(_prepare(root, desired, store))
        parent = root / "d000"
        if change == "membership":
            (parent / "user.txt").write_bytes(b"user")
        elif change == "mode":
            parent.chmod(0o700)
        elif change == "identity":
            parent.rename(root / "old-parent")
            parent.mkdir()
            (root / "old-parent/nested").rename(parent / "nested")
        else:
            (root / next(iter(desired))).write_bytes(b"user-edit")
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)
        with pytest.raises(KernelError) as stale:
            WorkspaceTransactionRuntime(store, leases).publish(
                record.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        assert stale.value.code == "execution_plan_stale"
        assert store.load(record.transaction_id).state == "prepared"
        assert (root / next(iter(desired))).read_bytes() != b"after\n"


def test_original_parent_control_survives_file_image_read(tmp_path, monkeypatch):
    root, state = tmp_path / "workspace", tmp_path / "state"
    desired = _files(root, 1)
    error = TurnCancelled()
    active = False
    original = planner._read_existing

    def checkpoint():
        if active:
            raise error

    def cancel_during_read(*args, **kwargs):
        nonlocal active
        active = True
        return original(*args, **kwargs)

    monkeypatch.setattr(planner, "_read_existing", cancel_during_read)
    with SQLiteWorkspaceTransactionStore(state) as store:
        with pytest.raises(TurnCancelled) as cancelled:
            prepare_workspace_transaction_v2(
                root,
                desired,
                request_id="cancel",
                checkpoint=checkpoint,
                write_blob=store.put_blob,
                read_blob=store.blob,
            )
        assert cancelled.value is error
        assert store._db.execute("SELECT count(*) FROM workspace_transactions").fetchone() == (0,)
    assert (root / next(iter(desired))).read_bytes() == b"before\n"


def test_prepared_record_cannot_claim_external_effect(tmp_path):
    root, state = tmp_path / "workspace", tmp_path / "state"
    desired = _files(root, 1)
    with (
        SQLiteWorkspaceTransactionStore(state / "transactions") as store,
        WorkspaceLeaseStore(state / "leases.db") as leases,
    ):
        record = store.save(_prepare(root, desired, store))
        (root / next(iter(desired))).write_bytes(b"after\n")
        observed = WorkspaceTransactionRuntime(store, leases).reconcile(record.transaction_id, root)
        assert observed.state == "diverged" and observed.error_code == "delivery_unowned_effect"


def test_real_effect_before_journal_recovers_without_rewriting(tmp_path, monkeypatch):
    root, state = tmp_path / "workspace", tmp_path / "state"
    desired = _files(root, 2)
    with (
        SQLiteWorkspaceTransactionStore(state / "transactions") as store,
        WorkspaceLeaseStore(state / "leases.db") as leases,
    ):
        record = store.save(_prepare(root, desired, store))
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)
        runtime = WorkspaceTransactionRuntime(store, leases)

        def crash(point):
            if point == "effect_applied:0":
                raise RuntimeError("cut-after-effect")

        monkeypatch.setattr(filesystem, "_fault", crash)
        with pytest.raises(RuntimeError, match="cut-after-effect"):
            runtime.publish(
                record.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        before = (root / "d000/nested/leaf.txt").stat().st_ino
        observed = runtime.reconcile(record.transaction_id, root)
        assert observed.state == "interrupted" and observed.cursor == 1
        monkeypatch.setattr(filesystem, "_fault", lambda _: None)
        result = runtime.publish(
            record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
        )
        assert result.state == "published"
        assert (root / "d000/nested/leaf.txt").stat().st_ino == before
