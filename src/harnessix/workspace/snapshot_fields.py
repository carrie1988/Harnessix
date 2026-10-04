"""两代快照共用原资源事实校验；不参与摘要、代际分派或宿主 IO。"""

from __future__ import annotations

from typing import Protocol

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import ExternalRoot, PlatformKind, WorkspaceResourceObservation
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key


class SnapshotResourceFields(Protocol):
    platform: PlatformKind
    cwd: str
    external_roots: tuple[ExternalRoot, ...]
    resources: tuple[WorkspaceResourceObservation, ...]


def validate_snapshot_resources(snapshot: SnapshotResourceFields) -> None:
    """保留旧合同的根排序、规范路径、平台去重、授权与 cwd 绑定。"""
    roots = [root.location for root in snapshot.external_roots]
    resources = [(item.location, item.path, item.access) for item in snapshot.resources]
    if (
        roots != sorted(roots)
        or len(set(roots)) != len(roots)
        or len(set(resources)) != len(resources)
    ):
        raise ValueError("Workspace Snapshot包含重复根或资源")
    _validate_paths(snapshot)
    expected_order = sorted(
        snapshot.resources,
        key=lambda item: (
            item.location,
            path_comparison_key(item.path, snapshot.platform),
            item.access,
        ),
    )
    if list(snapshot.resources) != expected_order:
        raise ValueError("Workspace资源顺序不规范")
    _validate_access(snapshot, roots)
    cwd_observations = [
        item
        for item in snapshot.resources
        if item.location == "workspace" and item.path == snapshot.cwd and item.access == "read"
    ]
    if len(cwd_observations) != 1 or cwd_observations[0].kind != "directory":
        raise ValueError("Workspace Snapshot没有绑定cwd目录")


def _validate_paths(snapshot: SnapshotResourceFields) -> None:
    try:
        normalized_cwd = normalize_workspace_path(snapshot.cwd, snapshot.platform)
        normalized_resources = [
            normalize_workspace_path(item.path, snapshot.platform) for item in snapshot.resources
        ]
    except KernelError:
        raise ValueError("Workspace包含非法逻辑路径") from None
    if snapshot.cwd != normalized_cwd or any(
        item.path != normalized
        for item, normalized in zip(snapshot.resources, normalized_resources, strict=True)
    ):
        raise ValueError("Workspace包含非规范逻辑路径")
    keys = [
        (item.location, path_comparison_key(item.path, snapshot.platform), item.access)
        for item in snapshot.resources
    ]
    if len(set(keys)) != len(keys):
        raise ValueError("Workspace资源按平台语义重复")


def _validate_access(snapshot: SnapshotResourceFields, roots: list[str]) -> None:
    allowed = {"workspace", *roots}
    if any(item.location not in allowed for item in snapshot.resources):
        raise ValueError("资源引用了未绑定的外部根")
    external_access = {root.location: set(root.access) for root in snapshot.external_roots}
    if any(
        item.location != "workspace" and item.access not in external_access[item.location]
        for item in snapshot.resources
    ):
        raise ValueError("资源访问模式超出外部根授权")
