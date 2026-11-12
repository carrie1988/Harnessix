"""新建文件时由事务补建缺失的父目录（ADR 0137）：只补建、只清理自己建的、绝不跟随符号链接。

事务层不变量在两代计划（父目录作为显式资源／写入父目录历史）上各验证一次。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import filesystem
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.parent_directories import (
    planned_new_directories,
    remove_empty_directories,
)
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.contracts import WorkspaceLease
from harnessix.workspace.leases import WorkspaceLeaseStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="只有POSIX事务端口补建父目录")

NEW = DesiredWorkspaceFile(b"created\n", 0o644)
GENERATIONS = pytest.mark.parametrize("generation", [1, 2])

_CRASH_BEFORE_REPLACE = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore
transaction_root, lease_path, workspace, transaction_id = sys.argv[1:]
with SQLiteWorkspaceTransactionStore(Path(transaction_root)) as store:
    with WorkspaceLeaseStore(Path(lease_path)) as leases:
        runtime = WorkspaceTransactionRuntime(store, leases)
        record = store.load(UUID(transaction_id))
        lease = leases.acquire(record.plan.source.workspace_id, 'crash-worker', ttl_seconds=60)
        # 父目录已补建、临时文件已写好，进程在把文件换入之前被硬杀。
        os.replace = lambda *args, **kwargs: os._exit(87)
        runtime.publish(record.transaction_id, Path(workspace),
            approval_fingerprint=record.plan.fingerprint, lease=lease)
raise AssertionError('未到达硬退出切点')
"""


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
    umask = os.umask(0o022)
    try:
        with _transaction(tmp_path, root, desired, generation) as (_, runtime, record, lease):
            assert _tree(root) == ["src", "src/a.py"]
            published = _publish(runtime, record, root, lease)
            assert _publish(runtime, record, root, lease) == published
    finally:
        os.umask(umask)

    assert published.state == "published"
    assert _tree(root) == ["pkg", "pkg/other.py", "pkg/sub", "pkg/sub/new.py", "src", "src/a.py"]
    assert (root / "pkg/sub/new.py").read_bytes() == b"created\n"
    assert [(root / name).stat().st_mode & 0o7777 for name in ("pkg", "pkg/sub")] == [0o755] * 2


def test_new_directory_mode_is_narrowed_by_the_process_umask(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    umask = os.umask(0o077)
    try:
        with _transaction(tmp_path, root, {"private/new.py": NEW}, 1) as (
            _,
            runtime,
            record,
            lease,
        ):
            _publish(runtime, record, root, lease)
    finally:
        os.umask(umask)

    assert (root / "private").stat().st_mode & 0o7777 == 0o700


@GENERATIONS
def test_failed_create_removes_only_the_directories_it_made(
    tmp_path: Path, generation: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成员在换入文件前失败：本次补建的目录删净，原有目录（即使是空的）保留，工作区与之前相同。"""
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
            patch.setattr(os, "replace", fail)
            with pytest.raises(KernelError) as failed:
                _publish(runtime, record, root, lease)
        settled = store.load(record.transaction_id)

    assert failed.value.code == "delivery_storage_unavailable"
    assert _tree(root) == before
    assert (settled.state, settled.cursor) == ("interrupted", 0)


def test_directories_holding_a_published_file_stay_when_a_later_member_fails(
    tmp_path: Path,
) -> None:
    """发布到一半失败沿用原有语义：已发布的成员留在原处并如实记账，它所在的新目录不为空、不删除。"""
    root = _workspace(tmp_path)
    desired = {"new/a.py": NEW, "src/a.py": DesiredWorkspaceFile(b"changed\n", 0o644)}
    with _transaction(tmp_path, root, desired, 1) as (store, runtime, record, lease):
        first = runtime.publish_next(
            record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
        )
        assert (first.state, first.cursor) == ("publishing", 1)
        (root / "src/a.py").write_bytes(b"user edit\n")
        with pytest.raises(KernelError):
            _publish(runtime, record, root, lease)
        settled = store.load(record.transaction_id)

    assert (settled.state, settled.cursor) == ("diverged", 1)
    assert settled.error_code == "delivery_source_changed"
    assert (root / "new/a.py").read_bytes() == b"created\n"
    assert (root / "src/a.py").read_bytes() == b"user edit\n"


@GENERATIONS
@pytest.mark.parametrize("target", ["directory", "nowhere"])
def test_symlink_put_in_place_of_a_planned_directory_is_never_followed(
    tmp_path: Path, generation: int, target: str
) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with _transaction(tmp_path, root, {"new/x.py": NEW}, generation) as (
        store,
        runtime,
        record,
        lease,
    ):
        (root / "new").symlink_to(outside if target == "directory" else tmp_path / "nowhere")
        with pytest.raises(KernelError) as refused:
            _publish(runtime, record, root, lease)
        # 即使跳过发布前的快照复核，成员写入自己的no-follow链也拒绝该符号链接。
        with pytest.raises(ReadToolError) as denied:
            filesystem._apply(store, root, record.transaction_id, 0, record.plan.mutations[0])
        assert store.load(record.transaction_id).state == "prepared"

    assert refused.value.code == "workspace_path_denied"
    assert denied.value.code == "path_denied"
    assert list(outside.iterdir()) == [] and not (tmp_path / "nowhere").exists()
    assert (root / "new").is_symlink()


@GENERATIONS
def test_regular_file_put_in_place_of_a_planned_directory_is_refused(
    tmp_path: Path, generation: int
) -> None:
    root = _workspace(tmp_path)
    with _transaction(tmp_path, root, {"new/x.py": NEW}, generation) as (
        store,
        runtime,
        record,
        lease,
    ):
        (root / "new").write_bytes(b"a file now\n")
        with pytest.raises(KernelError) as refused:
            _publish(runtime, record, root, lease)
        with pytest.raises(ReadToolError) as denied:
            filesystem._apply(store, root, record.transaction_id, 0, record.plan.mutations[0])

    assert refused.value.code == "workspace_wrong_file_type"
    assert denied.value.code == "wrong_file_type"
    assert (root / "new").read_bytes() == b"a file now\n"


@GENERATIONS
def test_member_below_a_missing_directory_is_observed_as_absent(
    tmp_path: Path, generation: int
) -> None:
    """恢复只观察：父目录还没补建的新建成员就是“不存在”，不再是观察失败。"""
    root = _workspace(tmp_path)
    with _transaction(tmp_path, root, {"pkg/sub/new.py": NEW}, generation) as (
        _,
        runtime,
        record,
        _,
    ):
        assert runtime.reconcile(record.transaction_id, root) == record
    assert _tree(root) == ["src", "src/a.py"]


def test_existing_member_whose_directory_vanished_is_settled_as_a_third_fact(
    tmp_path: Path,
) -> None:
    """已有文件的成员不补建目录：目录在发布中途消失，成员就是“不存在”，按第三事实结算。"""
    root = _workspace(tmp_path)
    desired = {
        "a.txt": NEW,
        "src/a.py": DesiredWorkspaceFile(b"changed\n", 0o644),
    }
    with _transaction(tmp_path, root, desired, 1) as (store, runtime, record, lease):
        runtime.publish_next(
            record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
        )
        (root / "src/a.py").unlink()
        (root / "src").rmdir()
        with pytest.raises(KernelError):
            _publish(runtime, record, root, lease)
        settled = store.load(record.transaction_id)

    assert (settled.state, settled.cursor) == ("diverged", 1)
    assert settled.error_code == "delivery_source_changed"
    assert _tree(root) == ["a.txt"]


def test_process_killed_after_creating_directories_recovers_by_observation_and_resumes(
    tmp_path: Path,
) -> None:
    """硬退出发生在补建目录之后、换入文件之前：恢复不写入，留下的是空目录与事务自己的临时文件。"""
    root = _workspace(tmp_path)
    prepared = prepare_workspace_transaction(root, {"pkg/sub/new.py": NEW}, request_id="crash")
    transaction_root, lease_path = tmp_path / "state/transactions", tmp_path / "state/leases.db"
    with SQLiteWorkspaceTransactionStore(transaction_root) as store:
        record = store.save(prepared)
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_BEFORE_REPLACE,
            str(transaction_root),
            str(lease_path),
            str(root),
            str(record.transaction_id),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert crashed.returncode == 87, crashed.stderr
    leftovers = [path.name for path in (root / "pkg/sub").iterdir()]
    assert len(leftovers) == 1 and leftovers[0].startswith(
        f".harnessix-{record.transaction_id.hex}-0-"
    )

    with SQLiteWorkspaceTransactionStore(transaction_root) as store:
        with WorkspaceLeaseStore(lease_path) as leases:
            runtime = WorkspaceTransactionRuntime(store, leases)
            interrupted = runtime.reconcile(record.transaction_id, root)
            assert (interrupted.state, interrupted.cursor) == ("interrupted", 0)
            assert [path.name for path in (root / "pkg/sub").iterdir()] == leftovers
            lease = leases.acquire(record.plan.source.workspace_id, "crash-worker", ttl_seconds=60)
            published = _publish(runtime, record, root, lease)

    assert published.state == "published"
    assert (root / "pkg/sub/new.py").read_bytes() == b"created\n"


@GENERATIONS
def test_plan_names_exactly_the_missing_parents_of_created_files(
    tmp_path: Path, generation: int
) -> None:
    root = _workspace(tmp_path)
    desired = {
        "src/a.py": DesiredWorkspaceFile(b"changed\n", 0o644),
        "src/new/b.py": NEW,
        "docs/guide/intro.md": NEW,
        "top.txt": NEW,
    }
    with _transaction(tmp_path, root, desired, generation) as (store, _, record, _):
        assert planned_new_directories(record.plan, store.blob) == (
            "docs",
            "docs/guide",
            "src/new",
        )
        diff = build_workspace_diff(record.plan, store)

    assert diff.text.startswith(
        "new directory docs\nnew directory docs/guide\nnew directory src/new\n"
        "diff --harnessix a/docs/guide/intro.md b/docs/guide/intro.md\n"
    )
    assert diff.text.count("new directory ") == 3


@GENERATIONS
def test_plan_without_new_directories_has_the_same_diff_as_before(
    tmp_path: Path, generation: int
) -> None:
    root = _workspace(tmp_path)
    with _transaction(tmp_path, root, {"src/b.py": NEW}, generation) as (store, _, record, _):
        assert planned_new_directories(record.plan, store.blob) == ()
        diff = build_workspace_diff(record.plan, store)

    assert diff.text == (
        "diff --harnessix a/src/b.py b/src/b.py\nnew file mode 644\n"
        "--- /dev/null\n+++ b/src/b.py\n@@ -0,0 +1 @@\n+created\n"
    )


def test_cleanup_removes_only_listed_directories_that_are_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    for directory in (root / "a/b", root / "a/kept", root / "c/d", root / "gone", outside):
        directory.mkdir(parents=True)
    (root / "a/kept/user.txt").write_bytes(b"user\n")
    (root / "gone").rmdir()
    (root / "link").symlink_to(outside)

    remove_empty_directories(root, ("a", "a/b", "c", "c/d", "gone", "link"))

    assert _tree(root) == ["a", "a/kept", "a/kept/user.txt", "link"]
    assert (root / "link").is_symlink() and outside.is_dir()
