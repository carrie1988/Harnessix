from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot


def test_snapshot_binds_file_content_cwd_and_missing_parent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    target = root / "src/main.py"
    target.write_text("before", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        root,
        cwd="src",
        resources=(
            WorkspaceResourceRequest(path="src/main.py", access="write"),
            WorkspaceResourceRequest(path="src/new.py", access="write"),
        ),
    )
    assert snapshot.cwd == "src"
    assert snapshot.workspace_id != snapshot.revision
    assert [item.kind for item in snapshot.resources] == ["directory", "file", "missing"]
    verify_workspace_snapshot(snapshot, root)

    target.write_text("after", encoding="utf-8")
    with pytest.raises(KernelError) as error:
        verify_workspace_snapshot(snapshot, root)
    assert error.value.code == "execution_plan_stale"


def test_snapshot_rejects_duplicate_resources_by_platform_semantics(tmp_path: Path) -> None:
    (tmp_path / "Main.py").write_text("x", encoding="utf-8")
    platform = "windows" if os.name == "nt" else "posix"
    duplicate = "main.py" if platform == "windows" else "Main.py"
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            tmp_path,
            resources=(
                WorkspaceResourceRequest(path="Main.py", access="read"),
                WorkspaceResourceRequest(path=duplicate, access="read"),
            ),
        )
    assert error.value.code == "workspace_snapshot_duplicate"


def test_external_root_access_is_explicit_and_revision_bound(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    (external / "data.txt").write_text("external", encoding="utf-8")
    roots = {"cache": (external, ("read",))}
    snapshot = capture_workspace_snapshot(
        root,
        resources=(WorkspaceResourceRequest(location="cache", path="data.txt", access="read"),),
        external_roots=roots,
    )
    assert snapshot.external_roots[0].location == "cache"
    verify_workspace_snapshot(snapshot, root, external_roots=roots)
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            root,
            resources=(
                WorkspaceResourceRequest(location="cache", path="data.txt", access="write"),
            ),
            external_roots=roots,
        )
    assert error.value.code == "workspace_access_denied"


def test_missing_resource_requires_an_existing_bound_parent(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            tmp_path,
            resources=(WorkspaceResourceRequest(path="missing/new.py", access="write"),),
        )
    assert error.value.code == "workspace_parent_missing"


@pytest.mark.skipif(os.name != "posix", reason="POSIX链接语义")
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_posix_snapshot_rejects_links(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    target = root / "target"
    if kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            root,
            resources=(WorkspaceResourceRequest(path="target", access="read"),),
        )
    assert error.value.code in {"workspace_path_denied", "workspace_wrong_file_type"}


@pytest.mark.skipif(os.name != "nt", reason="Windows Junction语义")
def test_windows_snapshot_rejects_junction(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    result = subprocess.run(
        ["cmd", "/D", "/C", "mklink", "/J", str(root / "junction"), str(outside)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("当前Windows runner不能创建Junction")
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot(
            root,
            resources=(WorkspaceResourceRequest(path="junction", access="read"),),
        )
    assert error.value.code == "workspace_path_denied"


@pytest.mark.skipif(os.name != "nt", reason="Windows长路径语义")
def test_windows_snapshot_reads_and_revalidates_long_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    relative = "/".join(["segment" * 10] * 4) + "/main.py"
    target = root / Path(*relative.split("/"))
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    assert len(str(target)) > 260

    snapshot = capture_workspace_snapshot(
        root,
        resources=(WorkspaceResourceRequest(path=relative, access="write"),),
    )
    verify_workspace_snapshot(snapshot, root)
    target.write_text("after", encoding="utf-8")
    with pytest.raises(KernelError) as error:
        verify_workspace_snapshot(snapshot, root)
    assert error.value.code == "execution_plan_stale"


@pytest.mark.skipif(os.name != "nt", reason="Windows句柄所有权语义")
def test_windows_root_handle_blocks_workspace_replacement(tmp_path: Path) -> None:
    from harnessix.workspace.windows import WindowsWorkspaceRoot

    root = tmp_path / "repo"
    root.mkdir()
    port = WindowsWorkspaceRoot(root)
    try:
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "replacement")
    finally:
        port.close()
