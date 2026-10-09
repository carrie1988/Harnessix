"""复用原生观察器，叶和全部父目录共享一次捕获的正文与取消预算。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import Workspace
from harnessix.workspace.contracts import (
    ExternalRoot,
    PlatformKind,
    ResourceAccess,
    WorkspaceResourceObservation,
    WorkspaceResourceRequest,
)
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError
from harnessix.workspace.parent_closure_paths import parent_paths
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key
from harnessix.workspace.snapshot import (
    _ACCESS_ORDER,
    MAX_SNAPSHOT_TOTAL_BYTES,
    _digest,
    _native_platform,
    _NativeRoot,
    _open_native,
    _root_path_key,
    _snapshot_resource_requests,
)


@dataclass(frozen=True, slots=True)
class SnapshotCapture:
    scope: dict[str, object]
    resources: tuple[WorkspaceResourceObservation, ...]
    parents: tuple[WorkspaceResourceObservation, ...]
    requests: tuple[WorkspaceResourceRequest, ...]


def capture_workspace_binding(
    root: Path, *, platform: PlatformKind, checkpoint: Callable[[], None]
) -> dict[str, object]:
    """鲜读根作用域，不读取未参与根身份的成员；完整资源快照仍走原端口。"""
    if platform != "posix" or os.name != "posix":
        return capture_snapshot_facts(
            root,
            cwd=".",
            resources=(),
            external_roots=None,
            platform=platform,
            checkpoint=checkpoint,
        ).scope
    checkpoint()
    try:
        workspace = Workspace(root, path_max_bytes=4096, path_max_parts=128)
    except (OSError, ReadToolError, ValueError):
        raise KernelError("workspace_binding_invalid", "POSIX Workspace根绑定失败") from None
    with workspace:
        try:
            # 同一次完整 no-follow 链保留 FD；出口仍鲜读根路径及复核原 FD。
            # 不枚举目录，也不把本次身份缓存给后继检查点。
            with workspace.open(".", NativeReadOperation(checkpoint), directory=True) as descriptor:
                info = os.fstat(descriptor)
                return _binding_scope(workspace.root, (info.st_dev, info.st_ino), platform)
        except UpstreamCheckpointError as error:
            raise error.error from None
        except ReadToolError as error:
            raise KernelError(f"workspace_{error.code}", "Workspace资源观察失败") from None
        except OSError:
            raise KernelError("workspace_observation_failed", "Workspace资源观察失败") from None


def _binding_scope(
    path: Path, root_identity: tuple[object, ...], platform: PlatformKind
) -> dict[str, object]:
    root_path_digest = _digest(_root_path_key(path, platform))
    identity = _digest(root_identity)
    return {
        "platform": platform,
        "workspace_id": _digest(
            {
                "platform": platform,
                "root_path_digest": root_path_digest,
                "root_identity": identity,
            }
        ),
        "root_path_digest": root_path_digest,
        "root_identity": identity,
    }


class _Observations:
    """同次等价 read 观察只读取一次；其他 access 不得合并权限事实。"""

    def __init__(
        self,
        roots: Mapping[str, _NativeRoot],
        platform: PlatformKind,
        grants: Mapping[str, tuple[ResourceAccess, ...]],
        checkpoint: Callable[[], None],
    ) -> None:
        self.roots, self.platform, self.grants, self.checkpoint = (
            roots,
            platform,
            grants,
            checkpoint,
        )
        self.cache: dict[tuple[str, str, ResourceAccess], WorkspaceResourceObservation] = {}
        self.total_bytes = 0

    def observe(self, request: WorkspaceResourceRequest) -> WorkspaceResourceObservation:
        self.checkpoint()
        key = (request.location, path_comparison_key(request.path, self.platform), request.access)
        if key in self.cache:
            existing = self.cache[key]
            return WorkspaceResourceObservation(**{**existing.model_dump(), "path": request.path})
        root = self.roots.get(request.location)
        if root is None:
            raise KernelError("workspace_external_root_invalid", "资源引用未知外部根")
        if request.location != "workspace" and request.access not in self.grants[request.location]:
            raise KernelError("workspace_access_denied", "外部根未授予所需访问模式")
        native = root.observe(request.path, access=request.access, checkpoint=self.checkpoint)
        self.checkpoint()
        self.total_bytes += len(native.content or b"")
        if self.total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
            raise KernelError("workspace_snapshot_limit", "Workspace快照总量超过上限")
        observation = WorkspaceResourceObservation(
            location=request.location,
            path=request.path,
            access=request.access,
            kind=native.kind,
            identity=_digest(native.identity),
            size=native.size,
            content_sha256=(
                hashlib.sha256(native.content).hexdigest()
                if native.kind == "file" and native.content is not None
                else None
            ),
        )
        self.cache[key] = observation
        return observation


def capture_snapshot_facts(
    root: str | Path,
    *,
    cwd: str,
    resources: Sequence[WorkspaceResourceRequest],
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None,
    platform: PlatformKind | None,
    checkpoint: Callable[[], None],
) -> SnapshotCapture:
    checkpoint()
    selected = platform or _native_platform()
    cwd = normalize_workspace_path(cwd, selected)
    requested = _prepare_requests(resources, cwd, selected)
    if len(external_roots or {}) > 16:
        raise KernelError("workspace_snapshot_limit", "Workspace快照资源超过上限")
    with ExitStack() as stack:
        roots, contracts = _open_roots(
            Path(root), external_roots or {}, selected, stack, checkpoint
        )
        observer = _Observations(
            roots, selected, {item.location: item.access for item in contracts}, checkpoint
        )
        cwd_fact = observer.observe(WorkspaceResourceRequest(path=cwd, access="read"))
        if cwd_fact.kind != "directory":
            raise KernelError("workspace_cwd_invalid", "Workspace cwd不是目录")
        observations = tuple(observer.observe(item) for item in requested)
        paths = parent_paths(requested, selected)
        parents = tuple(
            observer.observe(WorkspaceResourceRequest(location=location, path=path, access="read"))
            for location, path in paths
        )
        binding = _binding_scope(
            roots["workspace"].path, roots["workspace"].root_identity, selected
        )
        checkpoint()
        return SnapshotCapture(
            {
                **binding,
                "cwd": cwd,
                "external_roots": [item.model_dump(mode="json") for item in contracts],
            },
            observations,
            parents,
            requested,
        )


def _prepare_requests(
    resources: Sequence[WorkspaceResourceRequest],
    cwd: str,
    platform: PlatformKind,
) -> tuple[WorkspaceResourceRequest, ...]:
    requested = _snapshot_resource_requests(resources, cwd, platform)
    normalized = tuple(
        WorkspaceResourceRequest(
            location=item.location,
            path=normalize_workspace_path(item.path, platform),
            access=item.access,
        )
        for item in requested
    )
    keys = [
        (item.location, path_comparison_key(item.path, platform), item.access)
        for item in normalized
    ]
    if len(set(keys)) != len(keys):
        raise KernelError("workspace_snapshot_duplicate", "Workspace快照资源重复")
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.location,
                path_comparison_key(item.path, platform),
                item.access,
            ),
        )
    )


def _open_roots(
    root: Path,
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]],
    platform: PlatformKind,
    stack: ExitStack,
    checkpoint: Callable[[], None],
) -> tuple[dict[str, _NativeRoot], tuple[ExternalRoot, ...]]:
    workspace = _open_native(root, platform)
    stack.callback(workspace.close)
    roots: dict[str, _NativeRoot] = {"workspace": workspace}
    contracts: list[ExternalRoot] = []
    for location, (path, access) in sorted(external_roots.items()):
        checkpoint()
        if (
            location == "workspace"
            or not access
            or len(set(access)) != len(access)
            or any(item not in _ACCESS_ORDER for item in access)
            or tuple(sorted(access, key=_ACCESS_ORDER.__getitem__)) != access
        ):
            raise KernelError("workspace_external_root_invalid", "外部Workspace根配置无效")
        external = _open_native(Path(path), platform)
        stack.callback(external.close)
        roots[location] = external
        try:
            contracts.append(
                ExternalRoot(
                    location=location,
                    path_digest=_digest(_root_path_key(external.path, platform)),
                    identity=_digest(external.root_identity),
                    access=access,
                )
            )
        except ValidationError:
            raise KernelError(
                "workspace_external_root_invalid", "外部Workspace根配置无效"
            ) from None
    return roots, tuple(contracts)
