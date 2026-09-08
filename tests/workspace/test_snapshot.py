from __future__ import annotations

import ctypes
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


@pytest.mark.skipif(os.name != "nt", reason="Windows快照稳定性语义")
def test_windows_snapshot_remains_stable_after_selected_files_are_reopened(
    tmp_path: Path,
) -> None:
    from harnessix.workspace.windows import WindowsWorkspaceRoot

    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/keep.py").write_bytes(b"old\n")
    (root / "remove.txt").write_bytes(b"remove\n")
    resources = (
        WorkspaceResourceRequest(path=".", access="read"),
        WorkspaceResourceRequest(path="remove.txt", access="write"),
        WorkspaceResourceRequest(path="src", access="read"),
        WorkspaceResourceRequest(path="src/keep.py", access="write"),
        WorkspaceResourceRequest(path="src/new.py", access="write"),
    )
    expected = capture_workspace_snapshot(root, resources=resources)

    for path in ("src/keep.py", "remove.txt"):
        native = WindowsWorkspaceRoot(root)
        try:
            observed = native.observe(path, access="write")
            assert observed.kind == "file"
            assert observed.content is not None
        finally:
            native.close()

    verify_workspace_snapshot(expected, root)


def test_directory_snapshot_ignores_unselected_member_content_change(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    member = root / "member.txt"
    member.write_bytes(b"one!")
    expected = capture_workspace_snapshot(
        root,
        resources=(WorkspaceResourceRequest(path="new.txt", access="write"),),
    )

    member.write_bytes(b"same")

    verify_workspace_snapshot(expected, root)


def test_directory_snapshot_detects_same_name_member_replacement(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    member = root / "member.txt"
    member.write_bytes(b"same")
    expected = capture_workspace_snapshot(
        root,
        resources=(WorkspaceResourceRequest(path="new.txt", access="write"),),
    )

    member.rename(tmp_path / "original.txt")
    member.write_bytes(b"same")

    with pytest.raises(KernelError) as error:
        verify_workspace_snapshot(expected, root)
    assert error.value.code == "execution_plan_stale"


def test_windows_stable_identity_excludes_volatile_metadata() -> None:
    from harnessix.workspace.windows import (
        WindowsWorkspaceRoot,
        _ByHandleFileInformation,
    )

    before = _ByHandleFileInformation()
    before.attributes = 0x00000020  # FILE_ATTRIBUTE_ARCHIVE
    before.volume_serial = 7
    before.file_index_high = 11
    before.file_index_low = 13
    before.links = 1
    before.size_low = 17
    before.write_time.low = 19
    after = _ByHandleFileInformation()
    ctypes.memmove(ctypes.byref(after), ctypes.byref(before), ctypes.sizeof(before))
    after.attributes = 0
    after.write_time.low = 23

    assert WindowsWorkspaceRoot._revision_identity(before) != (
        WindowsWorkspaceRoot._revision_identity(after)
    )
    assert WindowsWorkspaceRoot._stable_identity(before, directory=False) == (
        WindowsWorkspaceRoot._stable_identity(after, directory=False)
    )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("volume_serial", 29),
        ("file_index_low", 31),
        ("attributes", 0x00000001),  # FILE_ATTRIBUTE_READONLY
        ("links", 2),
        ("size_low", 37),
    ],
)
def test_windows_stable_file_identity_binds_execution_relevant_metadata(
    field: str, changed: int
) -> None:
    from harnessix.workspace.windows import (
        WindowsWorkspaceRoot,
        _ByHandleFileInformation,
    )

    before = _ByHandleFileInformation()
    before.volume_serial = 7
    before.file_index_high = 11
    before.file_index_low = 13
    before.links = 1
    before.size_low = 17
    after = _ByHandleFileInformation()
    ctypes.memmove(ctypes.byref(after), ctypes.byref(before), ctypes.sizeof(before))
    setattr(after, field, changed)

    assert WindowsWorkspaceRoot._stable_identity(before, directory=False) != (
        WindowsWorkspaceRoot._stable_identity(after, directory=False)
    )
