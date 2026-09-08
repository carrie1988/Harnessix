from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    PROTECTED_COMPONENTS,
    FileMode,
    WorkspaceFileVersion,
    WorkspaceMutation,
    WorkspaceTransactionPlan,
    workspace_transaction_plan_fingerprint,
)
from harnessix.tools.workspace import ReadOperation, Workspace
from harnessix.workspace.contracts import PlatformKind, WorkspaceResourceRequest
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot


@dataclass(frozen=True, slots=True)
class DesiredWorkspaceFile:
    content: bytes | None = field(repr=False)
    mode: FileMode | None = None

    def __post_init__(self) -> None:
        if self.content is None:
            if self.mode is not None:
                raise ValueError("删除文件不能指定模式")
        elif type(self.content) is not bytes or self.mode not in {0o644, 0o755}:
            raise ValueError("目标文件必须是bytes且模式为0644或0755")


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceTransaction:
    plan: WorkspaceTransactionPlan
    blobs: Mapping[str, bytes] = field(repr=False)


def prepare_workspace_transaction(
    root: str | Path,
    desired: Mapping[str, DesiredWorkspaceFile],
    *,
    request_id: str,
    transaction_id: UUID | None = None,
    now: datetime | None = None,
    platform: PlatformKind | None = None,
) -> PreparedWorkspaceTransaction:
    selected_platform = platform or _native_platform()
    checked_now = now or datetime.now(UTC)
    if checked_now.tzinfo is None or not 1 <= len(request_id) <= 128 or not desired:
        raise KernelError("delivery_plan_invalid", "Workspace事务规划参数无效")
    normalized: dict[str, DesiredWorkspaceFile] = {}
    comparison: set[str] = set()
    for supplied, target in desired.items():
        path = normalize_workspace_path(supplied, selected_platform)
        key = path_comparison_key(path, selected_platform)
        if path == "." or key in comparison or _protected(path):
            raise KernelError("delivery_path_denied", "Workspace事务路径不允许写入")
        comparison.add(key)
        normalized[path] = target
    resources: dict[tuple[str, str], WorkspaceResourceRequest] = {}
    for path in normalized:
        resources[(path, "write")] = WorkspaceResourceRequest(path=path, access="write")
        parts = path.split("/")[:-1]
        for index in range(len(parts) + 1):
            parent = "/".join(parts[:index]) or "."
            resources[(parent, "read")] = WorkspaceResourceRequest(path=parent, access="read")
    snapshot = capture_workspace_snapshot(
        root,
        resources=tuple(resources[key] for key in sorted(resources)),
        platform=selected_platform,
    )
    observations = {
        (item.path, item.access): item
        for item in snapshot.resources
        if item.location == "workspace"
    }
    mutations: list[WorkspaceMutation] = []
    blobs: dict[str, bytes] = {}
    for path, target in sorted(
        normalized.items(), key=lambda item: path_comparison_key(item[0], selected_platform)
    ):
        observed = observations[(path, "write")]
        if observed.kind == "directory":
            raise KernelError("delivery_path_denied", "Workspace事务目标不能是目录")
        if observed.kind == "file":
            before_body, before_mode = _read_existing(Path(root), path, selected_platform)
            before = _file_version(before_body, before_mode)
            if before.sha256 != observed.content_sha256 or before.size != observed.size:
                raise KernelError("delivery_source_changed", "Workspace事务来源读取期间变化")
            if before.sha256 is None:
                raise KernelError("delivery_source_changed", "Workspace事务来源摘要缺失")
            blobs[before.sha256] = before_body
        else:
            before = WorkspaceFileVersion(presence="absent", size=0)
        if target.content is None:
            after = WorkspaceFileVersion(presence="absent", size=0)
        else:
            after = _file_version(target.content, target.mode)
            if after.sha256 is None:
                raise KernelError("delivery_plan_invalid", "Workspace事务目标摘要缺失")
            blobs[after.sha256] = target.content
        if before == after:
            raise KernelError("delivery_no_change", "Workspace事务包含无变化文件")
        mutations.append(WorkspaceMutation(path=path, before=before, after=after))
    verify_workspace_snapshot(snapshot, root)
    candidate = WorkspaceTransactionPlan.model_construct(
        _fields_set=None,
        transaction_id=transaction_id or uuid4(),
        request_id=request_id,
        source=snapshot,
        mutations=tuple(mutations),
        created_at=checked_now,
        fingerprint="0" * 64,
    )
    plan = WorkspaceTransactionPlan(
        transaction_id=candidate.transaction_id,
        request_id=request_id,
        source=snapshot,
        mutations=tuple(mutations),
        created_at=checked_now,
        fingerprint=workspace_transaction_plan_fingerprint(candidate),
    )
    return PreparedWorkspaceTransaction(plan=plan, blobs=MappingProxyType(blobs))


def _file_version(body: bytes, mode: FileMode | None) -> WorkspaceFileVersion:
    import hashlib

    if (
        type(body) is not bytes
        or len(body) > MAX_TRANSACTION_FILE_BYTES
        or mode
        not in {
            0o644,
            0o755,
        }
    ):
        raise KernelError("delivery_plan_limit", "Workspace事务文件镜像无效或超过上限")
    return WorkspaceFileVersion(
        presence="file",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        mode=mode,
    )


def _read_existing(root: Path, path: str, platform: PlatformKind) -> tuple[bytes, FileMode]:
    if platform == "posix" and os.name == "posix":
        try:
            with Workspace(root, path_max_bytes=4096, path_max_parts=128) as workspace:
                with workspace.open(path, ReadOperation(), directory=False) as descriptor:
                    info = os.fstat(descriptor)
                    body = _read_all(descriptor)
                    actual_mode = stat.S_IMODE(info.st_mode)
                    if actual_mode == 0o644:
                        mode: FileMode = 0o644
                    elif actual_mode == 0o755:
                        mode = 0o755
                    else:
                        raise KernelError(
                            "delivery_metadata_unsupported",
                            "Workspace事务只支持0644或0755普通文件",
                        )
                    return body, mode
        except OSError:
            raise KernelError("delivery_source_changed", "Workspace事务来源读取失败") from None
    if platform == "windows" and os.name == "nt":
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        native = WindowsWorkspaceRoot(root)
        try:
            observed = native.observe(path, access="write")
            if observed.kind != "file" or observed.content is None:
                raise KernelError("delivery_source_changed", "Windows事务来源不再是普通文件")
            return observed.content, 0o644
        finally:
            native.close()
    raise KernelError("workspace_platform_unsupported", "Workspace事务平台与宿主不一致")


def _read_all(descriptor: int) -> bytes:
    body = bytearray()
    while True:
        chunk = os.read(descriptor, min(65_536, MAX_TRANSACTION_FILE_BYTES + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_TRANSACTION_FILE_BYTES:
            raise KernelError("delivery_plan_limit", "Workspace事务文件超过上限")


def _protected(path: str) -> bool:
    return any(
        component.casefold() in PROTECTED_COMPONENTS or component.casefold().startswith(".env")
        for component in path.split("/")
    )


def _native_platform() -> PlatformKind:
    if os.name == "posix":
        return "posix"
    if os.name == "nt":
        return "windows"
    raise KernelError("workspace_platform_unsupported", "Workspace事务平台不受支持")
