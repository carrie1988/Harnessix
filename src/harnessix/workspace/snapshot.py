from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Literal, Protocol

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import ReadOperation, Workspace, revision_state
from harnessix.workspace.contracts import (
    ExternalRoot,
    PlatformKind,
    ResourceAccess,
    WorkspaceResourceObservation,
    WorkspaceResourceRequest,
    WorkspaceSnapshot,
)
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key

MAX_SNAPSHOT_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_DIRECTORY_ENTRIES = 10_000
_ACCESS_ORDER = {"read": 0, "write": 1, "execute": 2}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class _NativeObservation(Protocol):
    kind: Literal["file", "directory", "missing"]
    identity: tuple[object, ...]
    content: bytes | None
    size: int


class _NativeRoot(Protocol):
    path: Path
    root_identity: tuple[object, ...]

    def observe(self, path: str, *, access: ResourceAccess) -> _NativeObservation: ...

    def close(self) -> None: ...


class _Observed:
    def __init__(
        self,
        kind: Literal["file", "directory", "missing"],
        identity: tuple[object, ...],
        content: bytes | None,
        size: int,
    ) -> None:
        self.kind = kind
        self.identity = identity
        self.content = content
        self.size = size


class _PosixRoot:
    root_identity: tuple[object, ...]

    def __init__(self, path: Path) -> None:
        try:
            self._workspace = Workspace(path, path_max_bytes=4096, path_max_parts=128)
            self.path = self._workspace.root
            with self._workspace.open(".", ReadOperation(), directory=True) as descriptor:
                info = os.fstat(descriptor)
                self.root_identity = (info.st_dev, info.st_ino)
                self._root_device = info.st_dev
        except (OSError, ReadToolError, ValueError):
            raise KernelError("workspace_binding_invalid", "POSIX Workspace根绑定失败") from None

    def observe(self, path: str, *, access: ResourceAccess) -> _Observed:
        parts = self._workspace.parts(path)
        parent = "/".join(parts[:-1]) or "."
        name = parts[-1] if parts else None
        operation = ReadOperation()
        try:
            if name is None:
                return self._observe_existing(".", operation, directory=True, access=access)
            with self._workspace.open(
                parent,
                operation,
                directory=True,
                same_device=self._root_device,
            ) as parent_fd:
                parent_info = os.fstat(parent_fd)
                try:
                    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return _Observed(
                        "missing",
                        (*revision_state(parent_info), name),
                        None,
                        0,
                    )
                if stat.S_ISLNK(before.st_mode):
                    raise KernelError("workspace_path_denied", "Workspace资源不能是符号链接")
                directory = stat.S_ISDIR(before.st_mode)
                if not directory and not stat.S_ISREG(before.st_mode):
                    raise KernelError("workspace_path_denied", "Workspace资源类型不受支持")
            return self._observe_existing(path, operation, directory=directory, access=access)
        except KernelError:
            raise
        except FileNotFoundError:
            raise KernelError("workspace_parent_missing", "缺失资源的父目录不存在") from None
        except ReadToolError as error:
            raise KernelError(f"workspace_{error.code}", "Workspace资源观察失败") from None
        except OSError:
            raise KernelError("workspace_observation_failed", "Workspace资源观察失败") from None

    def _observe_existing(
        self, path: str, operation: ReadOperation, *, directory: bool, access: ResourceAccess
    ) -> _Observed:
        with self._workspace.open(
            path,
            operation,
            directory=directory,
            same_device=self._root_device,
        ) as descriptor:
            info = os.fstat(descriptor)
            if access == "execute" and not directory and info.st_mode & 0o111 == 0:
                raise KernelError("workspace_execute_denied", "Workspace文件不可执行")
            if directory:
                entries: list[tuple[str, int, tuple[int, ...]]] = []
                with os.scandir(descriptor) as iterator:
                    for entry in iterator:
                        if len(entries) >= MAX_SNAPSHOT_DIRECTORY_ENTRIES:
                            raise KernelError(
                                "workspace_snapshot_limit", "Workspace目录观察超过条目上限"
                            )
                        child = entry.stat(follow_symlinks=False)
                        entries.append(
                            (entry.name, stat.S_IFMT(child.st_mode), revision_state(child))
                        )
                entries.sort()
                body = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()
                return _Observed(
                    "directory",
                    (*revision_state(info), hashlib.sha256(body).hexdigest()),
                    body,
                    len(entries),
                )
            if info.st_size > MAX_SNAPSHOT_FILE_BYTES:
                raise KernelError("workspace_snapshot_limit", "Workspace文件超过快照上限")
            content = bytearray()
            while True:
                operation.checkpoint()
                chunk = os.read(descriptor, min(65536, MAX_SNAPSHOT_FILE_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_SNAPSHOT_FILE_BYTES:
                    raise KernelError("workspace_snapshot_limit", "Workspace文件超过快照上限")
            return _Observed("file", revision_state(info), bytes(content), info.st_size)

    def close(self) -> None:
        self._workspace.close()


def _native_platform() -> PlatformKind:
    if os.name == "posix":
        return "posix"
    if os.name == "nt":
        return "windows"
    raise KernelError("workspace_platform_unsupported", "当前Workspace平台不受支持")


def _open_native(path: Path, platform: PlatformKind) -> _NativeRoot:
    if platform == "posix" and os.name == "posix":
        return _PosixRoot(path)
    if platform == "windows" and os.name == "nt":
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        return WindowsWorkspaceRoot(path)
    raise KernelError("workspace_platform_unsupported", "请求平台与当前宿主不一致")


def capture_workspace_snapshot(
    root: str | Path,
    *,
    cwd: str = ".",
    resources: Sequence[WorkspaceResourceRequest] = (),
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
    platform: PlatformKind | None = None,
) -> WorkspaceSnapshot:
    """对计划涉及的根、cwd和资源生成有界内容快照。"""

    selected_platform = platform or _native_platform()
    normalized_cwd = normalize_workspace_path(cwd, selected_platform)
    if len(resources) > 256 or len(external_roots or {}) > 16:
        raise KernelError("workspace_snapshot_limit", "Workspace快照资源超过上限")
    with ExitStack() as stack:
        roots: dict[str, _NativeRoot] = {}
        workspace = _open_native(Path(root), selected_platform)
        roots["workspace"] = workspace
        stack.callback(workspace.close)
        external_contracts: list[ExternalRoot] = []
        for location, (path, access) in sorted((external_roots or {}).items()):
            if (
                location == "workspace"
                or not access
                or len(set(access)) != len(access)
                or any(item not in _ACCESS_ORDER for item in access)
                or tuple(sorted(access, key=_ACCESS_ORDER.__getitem__)) != access
            ):
                raise KernelError("workspace_external_root_invalid", "外部Workspace根配置无效")
            external = _open_native(Path(path), selected_platform)
            roots[location] = external
            stack.callback(external.close)
            external_contracts.append(
                ExternalRoot(
                    location=location,
                    path_digest=_digest(_root_path_key(external.path, selected_platform)),
                    identity=_digest(external.root_identity),
                    access=access,
                )
            )
        cwd_observation = workspace.observe(normalized_cwd, access="read")
        if cwd_observation.kind != "directory":
            raise KernelError("workspace_cwd_invalid", "Workspace cwd不是目录")
        requested = list(resources)
        cwd_key = ("workspace", path_comparison_key(normalized_cwd, selected_platform), "read")
        keys = {
            (item.location, path_comparison_key(item.path, selected_platform), item.access)
            for item in requested
        }
        if cwd_key not in keys:
            requested.append(
                WorkspaceResourceRequest(location="workspace", path=normalized_cwd, access="read")
            )
        observations: list[WorkspaceResourceObservation] = []
        total_bytes = 0
        seen: set[tuple[str, str, str]] = set()
        for request in requested:
            path = normalize_workspace_path(request.path, selected_platform)
            key = (request.location, path_comparison_key(path, selected_platform), request.access)
            if key in seen:
                raise KernelError("workspace_snapshot_duplicate", "Workspace快照资源重复")
            seen.add(key)
            native = roots.get(request.location)
            if native is None:
                raise KernelError("workspace_external_root_invalid", "资源引用未知外部根")
            allowed = (
                {"read", "write", "execute"}
                if request.location == "workspace"
                else set(
                    next(
                        root.access
                        for root in external_contracts
                        if root.location == request.location
                    )
                )
            )
            if request.access not in allowed:
                raise KernelError("workspace_access_denied", "外部根未授予所需访问模式")
            observed = native.observe(path, access=request.access)
            total_bytes += len(observed.content or b"")
            if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                raise KernelError("workspace_snapshot_limit", "Workspace快照总量超过上限")
            observations.append(
                WorkspaceResourceObservation(
                    location=request.location,
                    path=path,
                    access=request.access,
                    kind=observed.kind,
                    identity=_digest(observed.identity),
                    content_sha256=(
                        hashlib.sha256(observed.content).hexdigest()
                        if observed.kind == "file" and observed.content is not None
                        else None
                    ),
                    size=observed.size,
                )
            )
        observations.sort(
            key=lambda item: (
                item.location,
                path_comparison_key(item.path, selected_platform),
                item.access,
            )
        )
        root_path_digest = _digest(_root_path_key(workspace.path, selected_platform))
        root_identity = _digest(workspace.root_identity)
        workspace_id = _digest(
            {
                "platform": selected_platform,
                "root_path_digest": root_path_digest,
                "root_identity": root_identity,
            }
        )
        payload = {
            "algorithm": "selected-resources-sha256/v1",
            "platform": selected_platform,
            "workspace_id": workspace_id,
            "root_path_digest": root_path_digest,
            "root_identity": root_identity,
            "cwd": normalized_cwd,
            "external_roots": [item.model_dump(mode="json") for item in external_contracts],
            "resources": [item.model_dump(mode="json") for item in observations],
        }
        return WorkspaceSnapshot(
            platform=selected_platform,
            workspace_id=workspace_id,
            root_path_digest=root_path_digest,
            root_identity=root_identity,
            cwd=normalized_cwd,
            external_roots=tuple(external_contracts),
            resources=tuple(observations),
            revision=_digest(payload),
        )


def verify_workspace_snapshot(
    expected: WorkspaceSnapshot,
    root: str | Path,
    *,
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
) -> WorkspaceSnapshot:
    requests = tuple(
        WorkspaceResourceRequest(location=item.location, path=item.path, access=item.access)
        for item in expected.resources
        if not (
            item.location == "workspace" and item.path == expected.cwd and item.access == "read"
        )
    )
    current = capture_workspace_snapshot(
        root,
        cwd=expected.cwd,
        resources=requests,
        external_roots=external_roots,
        platform=expected.platform,
    )
    if current != expected:
        raise KernelError("execution_plan_stale", "Workspace Snapshot已变化")
    return current


def _root_path_key(path: Path, platform: PlatformKind) -> str:
    value = str(path)
    return value.casefold() if platform == "windows" else value
