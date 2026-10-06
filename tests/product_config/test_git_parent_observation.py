"""新来源最终观察保留净零路径、完整父成员与原调用方控制。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceFileVersion
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_delivery_source as source_module
from harnessix.product_config.git_delivery_source import (
    _observe_final_versions,
    _verify_final_snapshot,
)
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.product_config.test_git_delivery_source import raises_code


def fixture(root):
    root.mkdir()
    path = "a/b/c/file.py"
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same\n")
    target.chmod(0o644)
    version = WorkspaceFileVersion(
        presence="file", sha256=hashlib.sha256(b"same\n").hexdigest(), size=5, mode=0o644
    )
    return path, capture_workspace_snapshot(root), {path: (version, version)}


def test_net_zero_is_observed_and_history_is_durable_in_original_cas(tmp_path):
    root = tmp_path / "workspace"
    path, base, versions = fixture(root)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        ports = WorkspaceSnapshotPorts(store.put_blob, store.blob)
        snapshot = _observe_final_versions(root, base, versions, lambda: None, ports)
        assert {item.path for item in snapshot.resources} == {".", path}
        assert (
            len(read_workspace_parent_closure(snapshot, store.blob, checkpoint=lambda: None)) == 4
        )
        assert store._db.total_changes == 1  # 仅Store初始化的原schema元数据，没有事务事件。
        assert store._db.execute(
            "SELECT COUNT(*) FROM workspace_transaction_events"
        ).fetchone() == (0,)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state", read_only=True) as reopened:
        _verify_final_snapshot(
            snapshot, root, lambda: None, WorkspaceSnapshotPorts(reopened.put_blob, reopened.blob)
        )
        with raises_code("workspace_closure_unavailable"):
            _verify_final_snapshot(snapshot, root, lambda: None, None)


@pytest.mark.parametrize("drift", ["parent_member", "parent_mode", "leaf", "identity"])
def test_parent_or_leaf_drift_between_capture_and_final_verify_is_rejected(
    tmp_path, monkeypatch, drift
):
    root = tmp_path / "workspace"
    path, base, versions = fixture(root)
    original = source_module._read_existing

    def changed(*args, **kwargs):
        body = original(*args, **kwargs)
        if drift == "parent_member":
            (root / "a/b/new.txt").write_bytes(b"foreign\n")
        elif drift == "parent_mode":
            (root / "a/b").chmod(0o700)
        elif drift == "leaf":
            (root / path).write_bytes(b"other\n")
        else:
            directory = root / "a/b/c"
            moved = root / "a/b/old"
            directory.rename(moved)
            directory.mkdir()
            (directory / "file.py").write_bytes(b"same\n")
        return body

    monkeypatch.setattr(source_module, "_read_existing", changed)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        with pytest.raises(KernelError):
            _observe_final_versions(
                root,
                base,
                versions,
                lambda: None,
                WorkspaceSnapshotPorts(store.put_blob, store.blob),
            )
        assert store._db.execute(
            "SELECT COUNT(*) FROM workspace_transaction_events"
        ).fetchone() == (0,)


def test_original_cancellation_after_cas_write_is_not_success_or_automatic_retry(tmp_path):
    root = tmp_path / "workspace"
    _, base, versions = fixture(root)
    token, writes = CancelToken(), []
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:

        def write(digest, body):
            store.put_blob(digest, body)
            writes.append(digest)
            token.cancel()

        with pytest.raises(TurnCancelled):
            _observe_final_versions(
                root, base, versions, token.checkpoint, WorkspaceSnapshotPorts(write, store.blob)
            )
        assert len(writes) == 1
        assert store._db.execute(
            "SELECT COUNT(*) FROM workspace_transaction_events"
        ).fetchone() == (0,)


def test_original_control_exception_during_native_body_read_is_preserved(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    _, base, versions = fixture(root)
    original, error, reading = source_module._read_existing, RuntimeError("parent-control"), False

    def checkpoint():
        if reading:
            raise error

    def read(*args, **kwargs):
        nonlocal reading
        reading = True
        return original(*args, **kwargs)

    monkeypatch.setattr(source_module, "_read_existing", read)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        with pytest.raises(RuntimeError) as caught:
            _observe_final_versions(
                root, base, versions, checkpoint, WorkspaceSnapshotPorts(store.put_blob, store.blob)
            )
        assert caught.value is error


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_missing_or_corrupt_original_history_cannot_be_recaptured_as_success(tmp_path, fault):
    root = tmp_path / "workspace"
    _, base, versions = fixture(root)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        ports = WorkspaceSnapshotPorts(store.put_blob, store.blob)
        snapshot = _observe_final_versions(root, base, versions, lambda: None, ports)
        writes = []

        def unavailable(_digest):
            if fault == "missing":
                raise KernelError("delivery_blob_missing", "Blob不存在")
            return b"wrong"

        bad = WorkspaceSnapshotPorts(lambda digest, body: writes.append(digest), unavailable)
        with pytest.raises(KernelError):
            _verify_final_snapshot(snapshot, root, lambda: None, bad)
        assert writes == []
