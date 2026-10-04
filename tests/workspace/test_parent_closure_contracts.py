"""校验独立新代际与路径字典语义，不冒充 Windows 原生运行证据。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.contracts import WorkspaceResourceRequest, WorkspaceSnapshot
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.parent_closure_contracts import WorkspaceParentPathNode
from harnessix.workspace.parent_closure_paths import (
    decode_path_nodes,
    parent_paths,
    path_node_payloads,
)
from harnessix.workspace.parent_closure_wire import canonical_bytes, observations_digest
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_dictionary_roundtrip_keeps_every_shared_parent(platform) -> None:
    requests = (
        WorkspaceResourceRequest(path="A/B/c.txt", access="write"),
        WorkspaceResourceRequest(path="a/b/d.txt", access="write"),
        WorkspaceResourceRequest(path="!dir/f.txt", access="read"),
        WorkspaceResourceRequest(path=".", access="read"),
    )
    paths = parent_paths(requests, platform)
    nodes = tuple(WorkspaceParentPathNode(**item) for item in path_node_payloads(paths, platform))
    assert decode_path_nodes(nodes, platform) == paths
    assert paths[0] == ("workspace", ".")
    assert len(paths) == (6 if platform == "posix" else 4)
    assert parent_paths(tuple(reversed(requests)), platform) == paths


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "/escape", "CON", "a:stream", "a."])
def test_windows_dictionary_rejects_escape_and_platform_aliases(name: str) -> None:
    nodes = (
        WorkspaceParentPathNode(parent=None, name=".", location="workspace"),
        WorkspaceParentPathNode(parent=0, name=name, location=None),
    )
    with pytest.raises((ValueError, KernelError)):
        decode_path_nodes(nodes, "windows")


@pytest.mark.parametrize(
    "field,value", [("parent", -1), ("parent", True), ("name", ""), ("location", "WORKSPACE")]
)
def test_dictionary_node_fields_are_strict(field: str, value: object) -> None:
    payload = {"parent": None, "name": ".", "location": "workspace", field: value}
    with pytest.raises(ValidationError):
        WorkspaceParentPathNode.model_validate(payload, strict=True)


def test_stream_digest_equals_full_canonical_json(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir/file.txt").write_bytes(b"body\n")
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        # CAS 必须在另一物理根，不能在捕获后修改被保护目录。
        root = tmp_path / "dir"
        snapshot = capture_workspace_snapshot_v2(
            root,
            resources=[WorkspaceResourceRequest(path="file.txt", access="read")],
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        observations = read_workspace_parent_closure(
            snapshot, store._read_blob, checkpoint=lambda: None
        )
        whole = canonical_bytes([item.model_dump(mode="json") for item in observations])
        assert observations_digest(observations, lambda: None) == hashlib.sha256(whole).hexdigest()
        with pytest.raises(ValidationError):
            WorkspaceSnapshot.model_validate_json(snapshot.model_dump_json(), strict=True)


def test_reader_revalidates_model_copy_before_any_cas_read(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        bypass = snapshot.model_copy(update={"revision": "0" * 64})

        def forbidden_read(_digest):
            raise AssertionError("不完整快照不能访问 CAS")

        with pytest.raises(KernelError) as error:
            read_workspace_parent_closure(bypass, forbidden_read, checkpoint=lambda: None)
        assert error.value.code == "workspace_closure_corrupt"
