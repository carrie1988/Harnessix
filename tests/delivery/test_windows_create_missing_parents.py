"""Windows事务端口新建文件时补建缺失的父目录（ADR 0137第2版，Beta Issue #16）。

0.10.0b32 Windows：首个补丁在空仓库里新建`replenish/__init__.py`，以`workspace_parent_missing`失败，
Agent随后只用文字请求建目录，本轮显示“已完成”而仓库是空的。macOS与Linux自0.10.0b21起补建父目录；
Windows的观察与写入经原生句柄实现，当时没有可运行的环境验证，没有改。这里与POSIX的
`tests/delivery/test_create_missing_parents.py`逐项对应，只在支持事务端口的Windows 11上运行。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.parent_directories import (
    planned_new_directories,
    remove_empty_directories,
)
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.windows_io import windows_transaction_supported
from harnessix.workspace.contracts import WorkspaceLease
from harnessix.workspace.leases import WorkspaceLeaseStore

pytestmark = pytest.mark.skipif(
    not windows_transaction_supported(), reason="Windows 11原生事务端口才补建父目录"
)

NEW = DesiredWorkspaceFile(b"created\n", 0o644)
GENERATIONS = pytest.mark.parametrize("generation", [1, 2])


@contextmanager
def _transaction(
    tmp_path: Path, root: Path, desired: dict[str, DesiredWorkspaceFile], generation: int
) -> Iterator[
    tuple[
        SQLiteWorkspaceTransactionStore,
        WorkspaceTransactionRuntime,
        WorkspaceTransactionRecord,
        WorkspaceLease,
    ]
]:
    with SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions") as store:
        with WorkspaceLeaseStore(tmp_path / "state/leases.db") as leases:
            if generation == 1:
                prepared = prepare_workspace_transaction(root, desired, request_id="parents")
            else:
                prepared = prepare_workspace_transaction_v2(
                    root,
                    desired,
                    request_id="parents",
                    checkpoint=lambda: None,
                    write_blob=store.put_blob,
                    read_blob=store.blob,
                )
            record = store.save(prepared)
            lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)
            yield store, WorkspaceTransactionRuntime(store, leases), record, lease


def _publish(
    runtime: WorkspaceTransactionRuntime,
    record: WorkspaceTransactionRecord,
    root: Path,
    lease: WorkspaceLease,
) -> WorkspaceTransactionRecord:
    return runtime.publish(
        record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
    )


def _tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_bytes(b"old\n")
    return root


@GENERATIONS
def test_create_builds_every_missing_parent_directory(tmp_path: Path, generation: int) -> None:
    root = _workspace(tmp_path)
    desired = {"pkg/sub/new.py": NEW, "pkg/other.py": NEW, "src/a.py": NEW}
    with _transaction(tmp_path, root, desired, generation) as (store, runtime, record, lease):
        assert _tree(root) == ["src", "src/a.py"]
        assert planned_new_directories(record.plan, store.blob) == ("pkg", "pkg/sub")
        published = _publish(runtime, record, root, lease)
        assert _publish(runtime, record, root, lease) == published

    assert published.state == "published"
    assert _tree(root) == ["pkg", "pkg/other.py", "pkg/sub", "pkg/sub/new.py", "src", "src/a.py"]
    assert (root / "pkg/sub/new.py").read_bytes() == b"created\n"


@GENERATIONS
def test_failed_create_removes_only_the_directories_it_made(
    tmp_path: Path, generation: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成员在换入文件前失败：本次补建的目录删净，原有目录（即使是空的）保留，工作区与之前相同。"""
    from harnessix.workspace.windows_file_io import WindowsFileOperations

    root = _workspace(tmp_path)
    (root / "keep").mkdir()
    before = _tree(root)

    def fail(*_: object, **__: object) -> None:
        raise OSError("磁盘已满")

    with _transaction(tmp_path, root, {"keep/new/deep/x.py": NEW}, generation) as (
        store,
        runtime,
        record,
        lease,
    ):
        with monkeypatch.context() as patch:
            patch.setattr(WindowsFileOperations, "write_and_flush", fail)
            with pytest.raises(KernelError) as failed:
                _publish(runtime, record, root, lease)
        settled = store.load(record.transaction_id)

    assert failed.value.code == "delivery_storage_unavailable"
    assert _tree(root) == before
    assert (settled.state, settled.cursor) == ("interrupted", 0)


@GENERATIONS
def test_member_below_a_missing_directory_is_observed_as_absent(
    tmp_path: Path, generation: int
) -> None:
    """恢复只观察：父目录还没补建的新建成员就是“不存在”，不是观察失败。"""
    root = _workspace(tmp_path)
    with _transaction(tmp_path, root, {"pkg/sub/new.py": NEW}, generation) as (
        _,
        runtime,
        record,
        _,
    ):
        assert runtime.reconcile(record.transaction_id, root) == record
    assert _tree(root) == ["src", "src/a.py"]


@pytest.mark.parametrize("path", ["plain.txt/new.py", "plain.txt/deeper/new.py"])
def test_existing_parent_that_is_a_regular_file_is_refused_at_planning(
    tmp_path: Path, path: str
) -> None:
    root = _workspace(tmp_path)
    (root / "plain.txt").write_bytes(b"plain\n")
    with pytest.raises(KernelError) as refused:
        prepare_workspace_transaction(root, {path: NEW}, request_id="parents")
    assert refused.value.code == "workspace_wrong_file_type"
    assert _tree(root) == ["plain.txt", "src", "src/a.py"]


def test_one_path_used_as_file_and_as_new_directory_is_refused_at_planning(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(KernelError) as refused:
        prepare_workspace_transaction(root, {"Pkg": NEW, "pkg/a.py": NEW}, request_id="parents")
    assert refused.value.code == "delivery_path_case_conflict"
    assert _tree(root) == ["src", "src/a.py"]


def test_cleanup_removes_only_listed_directories_that_are_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    for directory in (root / "a/b", root / "a/kept", root / "c/d", root / "gone"):
        directory.mkdir(parents=True)
    (root / "a/kept/user.txt").write_bytes(b"user\n")
    (root / "gone").rmdir()

    remove_empty_directories(root, ("a", "a/b", "c", "c/d", "gone"))

    assert _tree(root) == ["a", "a/kept", "a/kept/user.txt"]
    assert os.path.isdir(root / "a/kept")
