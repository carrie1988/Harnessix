"""以真实分散叶和原私有 CAS 验证完整父目录快照，不替换原生观察。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_v2 import (
    capture_workspace_snapshot_v2,
    verify_workspace_snapshot_v2,
)


def _spread(root: Path, count: int) -> tuple[WorkspaceResourceRequest, ...]:
    root.mkdir()
    requests = []
    for index in range(count):
        directory = root / f"d{index:03}"
        directory.mkdir()
        (directory / "file.txt").write_bytes(b"before\n")
        requests.append(WorkspaceResourceRequest(path=f"d{index:03}/file.txt", access="write"))
    return tuple(requests)


@pytest.mark.parametrize("count", [127, 128, 255])
def test_full_parent_history_survives_readonly_reopen(tmp_path: Path, count: int) -> None:
    root, state = tmp_path / "workspace", tmp_path / "state"
    requests = _spread(root, count)
    with SQLiteWorkspaceTransactionStore(state) as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=requests,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        assert len(snapshot.resources) == count + 1
        assert snapshot.parent_closure.parent_count == count + 1
        original = read_workspace_parent_closure(
            snapshot, store._read_blob, checkpoint=lambda: None
        )
        assert {item.path for item in original} == {".", *(f"d{i:03}" for i in range(count))}
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as reopened:
        assert (
            read_workspace_parent_closure(
                snapshot,
                reopened._read_blob,
                checkpoint=lambda: None,
            )
            == original
        )
        assert (
            verify_workspace_snapshot_v2(
                snapshot,
                root,
                checkpoint=lambda: None,
                read_blob=reopened._read_blob,
            )
            == snapshot
        )


def test_original_inline_representation_still_rejects_128_spread_leaves(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    requests = _spread(root, 128)
    parents = tuple(WorkspaceResourceRequest(path=f"d{i:03}", access="read") for i in range(128))
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(root, resources=(*requests, *parents))
    assert error.value.code == "workspace_snapshot_limit"


def test_request_order_and_explicit_cwd_have_identical_wire(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    requests = _spread(root, 12)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        first = capture_workspace_snapshot_v2(
            root,
            resources=requests,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        before = {item.name: item.read_bytes() for item in store._blobs.iterdir()}
        second = capture_workspace_snapshot_v2(
            root,
            resources=(*reversed(requests), WorkspaceResourceRequest(path=".", access="read")),
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        assert second == first
        assert {item.name: item.read_bytes() for item in store._blobs.iterdir()} == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            "parent-mode", marks=pytest.mark.skipif(os.name != "posix", reason="POSIX目录权限位")
        ),
        "parent-object",
        "parent-members",
        "leaf-body",
        "root",
    ],
)
def test_changed_facts_are_stale_not_resigned(tmp_path: Path, change: str) -> None:
    root = tmp_path / "workspace"
    requests = _spread(root, 3)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=requests,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        before = {item.name: item.read_bytes() for item in store._blobs.iterdir()}
        if change == "parent-mode":
            (root / "d001").chmod(0o700)
        elif change == "parent-object":
            (root / "d001").rename(tmp_path / "old-parent")
            (root / "d001").mkdir()
            (root / "d001/file.txt").write_bytes(b"before\n")
        elif change == "parent-members":
            (root / "d001/other.txt").write_bytes(b"not-selected\n")
        elif change == "leaf-body":
            (root / "d001/file.txt").write_bytes(b"changed\n")
        else:
            root = tmp_path / "other-root"
            _spread(root, 3)
        with pytest.raises(KernelError) as error:
            verify_workspace_snapshot_v2(
                snapshot,
                root,
                checkpoint=lambda: None,
                read_blob=store._read_blob,
            )
        assert error.value.code == "execution_plan_stale"
        assert {item.name: item.read_bytes() for item in store._blobs.iterdir()} == before


def test_shared_deep_parent_chain_retains_every_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    prefix = "/".join(f"level{i:02}" for i in range(40))
    (root / prefix).mkdir(parents=True)
    requests = []
    for index in range(255):
        path = f"{prefix}/file{index:03}.txt"
        (root / path).write_bytes(b"body\n")
        requests.append(WorkspaceResourceRequest(path=path, access="write"))
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=requests,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        parents = read_workspace_parent_closure(snapshot, store._read_blob, checkpoint=lambda: None)
        assert len(parents) == 41
        assert {item.path for item in parents} == {
            ".",
            *("/".join(prefix.split("/")[:i]) for i in range(1, 41)),
        }
        assert (
            verify_workspace_snapshot_v2(
                snapshot,
                root,
                checkpoint=lambda: None,
                read_blob=store._read_blob,
            )
            == snapshot
        )


def test_leaf_and_directory_bodies_share_original_32_mib_budget(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    requests = []
    for index in range(4):
        path = f"file{index}.txt"
        (root / path).write_bytes(b"a" * (8 * 1024 * 1024))
        requests.append(WorkspaceResourceRequest(path=path, access="write"))
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        with pytest.raises(KernelError) as error:
            capture_workspace_snapshot_v2(
                root,
                resources=requests,
                checkpoint=lambda: None,
                write_blob=store.put_blob,
                read_blob=store._read_blob,
            )
        assert error.value.code == "workspace_snapshot_limit"
        assert list(store._blobs.iterdir()) == []


def test_overflow_and_duplicate_do_not_prepare_blobs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    requests = _spread(root, 256)
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        for selected, code in [
            (requests, "workspace_snapshot_limit"),
            ((requests[0], requests[0]), "workspace_snapshot_duplicate"),
        ]:
            with pytest.raises(KernelError) as error:
                capture_workspace_snapshot_v2(
                    root,
                    resources=selected,
                    checkpoint=lambda: None,
                    write_blob=store.put_blob,
                    read_blob=store._read_blob,
                )
            assert error.value.code == code
        assert list(store._blobs.iterdir()) == []


def test_external_write_does_not_grant_parent_read(tmp_path: Path) -> None:
    root, external = tmp_path / "workspace", tmp_path / "external"
    root.mkdir()
    requests = _spread(external, 1)
    selected = [WorkspaceResourceRequest(location="cache", path=requests[0].path, access="write")]
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        with pytest.raises(KernelError) as error:
            capture_workspace_snapshot_v2(
                root,
                resources=selected,
                external_roots={"cache": (external, ("write",))},
                checkpoint=lambda: None,
                write_blob=store.put_blob,
                read_blob=store._read_blob,
            )
        assert error.value.code == "workspace_access_denied"
        assert list(store._blobs.iterdir()) == []


def test_external_scope_and_root_are_fully_bound(tmp_path: Path) -> None:
    root, external = tmp_path / "workspace", tmp_path / "external"
    root.mkdir()
    requests = _spread(external, 2)
    selected = [
        WorkspaceResourceRequest(location="cache", path=item.path, access="read")
        for item in requests
    ]
    roots = {"cache": (external, ("read",))}
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=selected,
            external_roots=roots,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        parents = read_workspace_parent_closure(snapshot, store._read_blob, checkpoint=lambda: None)
        assert {(item.location, item.path) for item in parents} == {
            ("workspace", "."),
            ("cache", "."),
            ("cache", "d000"),
            ("cache", "d001"),
        }
        assert (
            verify_workspace_snapshot_v2(
                snapshot,
                root,
                external_roots=roots,
                checkpoint=lambda: None,
                read_blob=store._read_blob,
            )
            == snapshot
        )


@pytest.mark.parametrize(
    "boundary",
    ["before-first-write", "after-chunk-write", "after-manifest-write", "read-confirmation"],
)
@pytest.mark.parametrize("signal", [TurnCancelled, TimeoutError])
def test_capture_control_never_returns_snapshot_or_business_record(
    tmp_path: Path, boundary: str, signal
) -> None:
    root = tmp_path / "workspace"
    requests = _spread(root, 2)
    error = signal()
    writes = []
    stopped = False
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:

        def checkpoint():
            if stopped:
                raise error

        def write(digest, body):
            nonlocal stopped
            if boundary == "before-first-write":
                raise error
            store.put_blob(digest, body)
            writes.append(digest)
            if (
                boundary == "after-chunk-write"
                or boundary == "after-manifest-write"
                and len(writes) == 2
            ):
                stopped = True

        def read(digest):
            if boundary == "read-confirmation" and writes:
                raise error
            return store._read_blob(digest)

        with pytest.raises(type(error)) as caught:
            capture_workspace_snapshot_v2(
                root,
                resources=requests,
                checkpoint=checkpoint,
                write_blob=write,
                read_blob=read,
            )
        assert caught.value is error
        assert store._db.execute("SELECT COUNT(*) FROM workspace_transactions").fetchone() == (0,)


def test_durable_confirmation_loss_keeps_only_unreferenced_evidence(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    requests = _spread(root, 2)
    error = KernelError("delivery_storage_unavailable", "durable-confirmation")
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:

        def write(digest, body):
            store.put_blob(digest, body)
            raise error

        with pytest.raises(KernelError) as caught:
            capture_workspace_snapshot_v2(
                root,
                resources=requests,
                checkpoint=lambda: None,
                write_blob=write,
                read_blob=store._read_blob,
            )
        assert caught.value is error
        assert len(list(store._blobs.iterdir())) == 1
        assert store._db.execute("SELECT COUNT(*) FROM workspace_transactions").fetchone() == (0,)


def test_root_node_precedes_punctuation_named_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "!dir").mkdir(parents=True)
    (root / "!dir/file.txt").write_bytes(b"data")
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=[WorkspaceResourceRequest(path="!dir/file.txt", access="read")],
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        assert [
            item.path
            for item in read_workspace_parent_closure(
                snapshot, store._read_blob, checkpoint=lambda: None
            )
        ] == [".", "!dir"]
