"""写目标可以位于尚不存在的父目录之下（ADR 0137）：快照如实记录缺失，已有路径段照旧校验。

两代快照共用同一原生观察：第1代把父目录作为显式read资源，第2代把它们写入父目录历史。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot import (
    _observe_below_missing_parent,
    _PosixRoot,
    capture_workspace_snapshot,
    verify_workspace_snapshot,
)
from harnessix.workspace.snapshot_v2 import (
    capture_workspace_snapshot_v2,
    verify_workspace_snapshot_v2,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="只有POSIX事务端口补建父目录")

WRITE = WorkspaceResourceRequest(path="pkg/sub/new.py", access="write")
PARENTS = (
    WorkspaceResourceRequest(path="pkg", access="read"),
    WorkspaceResourceRequest(path="pkg/sub", access="read"),
)


def _kinds(resources) -> dict[tuple[str, str], str]:
    return {(item.path, item.access): item.kind for item in resources}


def test_first_generation_records_the_write_target_and_its_missing_parents(tmp_path: Path) -> None:
    snapshot = capture_workspace_snapshot(tmp_path, resources=(WRITE, *PARENTS))

    assert _kinds(snapshot.resources) == {
        (".", "read"): "directory",
        ("pkg", "read"): "missing",
        ("pkg/sub", "read"): "missing",
        ("pkg/sub/new.py", "write"): "missing",
    }
    assert verify_workspace_snapshot(snapshot, tmp_path) == snapshot
    assert not (tmp_path / "pkg").exists()
    # 只请求写目标（不显式列出父目录）同样成立：放行由写目标推导，不依赖调用方列全祖先。
    alone = capture_workspace_snapshot(tmp_path, resources=(WRITE,))
    assert _kinds(alone.resources)[("pkg/sub/new.py", "write")] == "missing"


def test_second_generation_records_missing_parents_in_the_parent_history(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=(WRITE,),
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store.blob,
        )
        parents = read_workspace_parent_closure(snapshot, store.blob, checkpoint=lambda: None)

        assert _kinds(snapshot.resources)[("pkg/sub/new.py", "write")] == "missing"
        assert [(item.path, item.kind) for item in parents] == [
            (".", "directory"),
            ("pkg", "missing"),
            ("pkg/sub", "missing"),
        ]
        assert (
            verify_workspace_snapshot_v2(
                snapshot, root, checkpoint=lambda: None, read_blob=store.blob
            )
            == snapshot
        )
    assert not (root / "pkg").exists()


@pytest.mark.parametrize("appeared", ["directory", "file"])
def test_snapshot_goes_stale_when_a_missing_parent_appears(tmp_path: Path, appeared: str) -> None:
    """批准的是“这些目录不存在、由事务补建”；目录在批准后出现就不再是被批准的事实。"""
    snapshot = capture_workspace_snapshot(tmp_path, resources=(WRITE, *PARENTS))
    if appeared == "directory":
        (tmp_path / "pkg").mkdir()
    else:
        (tmp_path / "pkg").write_bytes(b"now a file\n")

    with pytest.raises(KernelError) as error:
        verify_workspace_snapshot(snapshot, tmp_path)

    assert error.value.code == (
        "execution_plan_stale" if appeared == "directory" else "workspace_wrong_file_type"
    )


def test_missing_identity_binds_the_nearest_existing_directory(tmp_path: Path) -> None:
    """身份仍绑定一个真实目录：最近的已有目录，加上其下自首个缺失段起的路径。"""
    (tmp_path / "src").mkdir()
    native = _PosixRoot(tmp_path)
    try:
        observed = _observe_below_missing_parent(native, "src/a/b/new.py", None)
        first_missing = native.observe("src/a", access="read")
    finally:
        native.close()
    parent = os.stat(tmp_path / "src")

    assert observed.kind == "missing" and observed.content is None and observed.size == 0
    assert first_missing.identity == (
        parent.st_dev,
        parent.st_ino,
        parent.st_mode,
        parent.st_nlink,
        parent.st_uid,
        parent.st_gid,
        "a",
    )
    assert observed.identity == (*first_missing.identity, "b", "new.py")


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("plain.txt/new.py", "workspace_wrong_file_type"),
        ("plain.txt/deeper/new.py", "workspace_wrong_file_type"),
        ("link/new.py", "workspace_path_denied"),
        ("link/deeper/new.py", "workspace_path_denied"),
        ("dangling/new.py", "workspace_path_denied"),
        ("pkg/../escape.py", "workspace_path_denied"),
        ("/tmp/escape.py", "workspace_path_denied"),
    ],
)
def test_existing_components_are_validated_before_anything_counts_as_missing(
    tmp_path: Path, path: str, code: str
) -> None:
    """缺失只对真正不存在的路径段成立：普通文件、符号链接、越界与敏感路径段照旧拒绝。"""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "plain.txt").write_bytes(b"plain\n")
    (root / "link").symlink_to(outside)
    (root / "dangling").symlink_to(tmp_path / "nowhere")

    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            root, resources=(WorkspaceResourceRequest(path=path, access="write"),)
        )

    assert error.value.code == code
    assert list(outside.iterdir()) == []


def test_sensitive_segment_below_a_missing_parent_is_denied_like_any_other(tmp_path: Path) -> None:
    """敏感名称由同一路径策略在任何观察之前拒绝，与父目录是否存在无关。"""
    with pytest.raises(ReadToolError) as error:
        capture_workspace_snapshot(
            tmp_path,
            resources=(WorkspaceResourceRequest(path="pkg/.git/config", access="write"),),
        )
    assert error.value.code == "path_denied"
    assert not (tmp_path / "pkg").exists()


def test_read_beside_a_write_under_the_same_missing_parent_is_still_rejected(
    tmp_path: Path,
) -> None:
    """放行只跟随写目标及其祖先，不扩散到同一缺失目录下的读取请求。"""
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            tmp_path,
            resources=(
                WorkspaceResourceRequest(path="pkg/new.py", access="write"),
                WorkspaceResourceRequest(path="pkg/other.py", access="read"),
            ),
        )
    assert error.value.code == "workspace_parent_missing"
