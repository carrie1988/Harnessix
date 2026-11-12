"""Workspace身份与租约：冻结已校验输入及其内容摘要。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol, Self

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS, ReadToolError
from harnessix.tools.workspace import ReadOperation, Workspace, revision_state
from harnessix.workspace.contracts import (
    ExternalRoot,
    PlatformKind,
    ResourceAccess,
    WorkspaceResourceObservation,
    WorkspaceResourceRequest,
    WorkspaceSnapshot,
)
from harnessix.workspace.native_observation_io import (
    NativeReadOperation,
    UpstreamCheckpointError,
    observe_directory,
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
    binding: tuple[object, ...]
    content: bytes | None
    size: int
    entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...] | None


class _NativeRoot(Protocol):
    path: Path
    root_identity: tuple[object, ...]

    def observe(
        self,
        path: str,
        *,
        access: ResourceAccess,
        checkpoint: Callable[[], None] | None = None,
    ) -> _NativeObservation: ...

    def close(self) -> None: ...


class _Observed:
    def __init__(
        self,
        kind: Literal["file", "directory", "missing"],
        identity: tuple[object, ...],
        content: bytes | None,
        size: int,
        entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...]
        | None = None,
        *,
        binding: tuple[object, ...] | None = None,
    ) -> None:
        self.kind = kind
        self.identity = identity
        # 计划快照绑定的身份（ADR 0143）：目录只认是哪个目录及其权限，不含成员；其余与观察身份相同。
        self.binding = identity if binding is None else binding
        self.content = content
        self.size = size
        self.entries = entries


def _read_operation(
    checkpoint: Callable[[], None] | None, timeout_seconds: int | float
) -> ReadOperation:
    """局部读取操作；有上游检查点时先消费上游。默认时限下的构造方式与原来完全一致。"""

    if timeout_seconds == READ_TIMEOUT_SECONDS:
        return ReadOperation() if checkpoint is None else NativeReadOperation(checkpoint)
    if checkpoint is None:
        return ReadOperation(timeout_seconds=timeout_seconds)
    return NativeReadOperation(checkpoint, timeout_seconds=timeout_seconds)


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

    def observe(
        self,
        path: str,
        *,
        access: ResourceAccess,
        checkpoint: Callable[[], None] | None = None,
        read_timeout_seconds: int | float = READ_TIMEOUT_SECONDS,
    ) -> _Observed:
        parts = self._workspace.parts(path)
        parent = "/".join(parts[:-1]) or "."
        name = parts[-1] if parts else None
        operation = _read_operation(checkpoint, read_timeout_seconds)
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
                    return _missing_below(parent_info, name)
                if stat.S_ISLNK(before.st_mode):
                    raise KernelError("workspace_path_denied", "Workspace资源不能是符号链接")
                directory = stat.S_ISDIR(before.st_mode)
                if not directory and not stat.S_ISREG(before.st_mode):
                    raise KernelError("workspace_path_denied", "Workspace资源类型不受支持")
            return self._observe_existing(path, operation, directory=directory, access=access)
        except UpstreamCheckpointError as error:
            raise error.error from None
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
                body, count, exposed = observe_directory(
                    descriptor,
                    max_entries=MAX_SNAPSHOT_DIRECTORY_ENTRIES,
                    checkpoint=operation.checkpoint
                    if isinstance(operation, NativeReadOperation)
                    else None,
                )
                return _Observed(
                    "directory",
                    (*self._stable_directory_identity(info), hashlib.sha256(body).hexdigest()),
                    body,
                    count,
                    exposed,
                    binding=_directory_binding(info),
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

    @staticmethod
    def _stable_directory_identity(info: os.stat_result) -> tuple[int, ...]:
        """目录跨观察身份只绑定执行相关元数据；成员变化由成员对象摘要绑定。"""

        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
        )

    def close(self) -> None:
        self._workspace.close()


def _directory_binding(info: os.stat_result) -> tuple[int, ...]:
    """计划对目录的绑定：对象与权限。成员数（APFS把它计入链接数）和成员列表不在其中（ADR 0143）。"""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _missing_below(parent: os.stat_result, name: str) -> _Observed:
    """父目录存在而名称不存在：观察身份与计划绑定都由父目录的身份加名称构成。"""
    return _Observed(
        "missing",
        (*_PosixRoot._stable_directory_identity(parent), name),
        None,
        0,
        binding=(*_directory_binding(parent), name),
    )


def bound_size(observed: _NativeObservation) -> int:
    """计划快照记录的大小：目录的大小是成员数，不绑定（ADR 0143）。"""
    return 0 if observed.kind == "directory" else observed.size


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


def creatable_resource_keys(
    requests: Sequence[WorkspaceResourceRequest], platform: PlatformKind
) -> frozenset[tuple[str, str]]:
    """写目标及其全部祖先的比较键：只有它们可以位于尚不存在的父目录之下（ADR 0137）。

    读取与执行请求仍要求父目录存在；祖先需要一并放行，否则缺失目录下的写目标无法绑定父目录历史。
    """
    keys: set[tuple[str, str]] = set()
    for request in requests:
        if request.access != "write":
            continue
        parts = normalize_workspace_path(request.path, platform).split("/")
        keys.update(
            (request.location, path_comparison_key("/".join(parts[:end]), platform))
            for end in range(1, len(parts) + 1)
        )
    return frozenset(keys)


def observe_creatable_resource(
    native: _NativeRoot,
    request: WorkspaceResourceRequest,
    rejection: KernelError,
    requests: Sequence[WorkspaceResourceRequest],
    platform: PlatformKind,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> _NativeObservation:
    """“父目录不存在”的拒绝只对本次快照可新建的资源改为缺失观察，其余原样抛出。

    只有POSIX事务端口会在发布时补建父目录；Windows端口保持“父目录必须存在”。
    """
    key = (request.location, path_comparison_key(request.path, platform))
    if (
        rejection.code != "workspace_parent_missing"
        or not isinstance(native, _PosixRoot)
        or key not in creatable_resource_keys(requests, platform)
    ):
        raise rejection
    return _observe_below_missing_parent(native, request.path, checkpoint)


def _observe_below_missing_parent(
    native: _PosixRoot, path: str, checkpoint: Callable[[], None] | None
) -> _Observed:
    """把父目录缺失的资源观察为缺失；身份绑定最近的已有目录及其下自首个缺失段起的路径。

    每个祖先仍经observe的同一条no-follow链，已有路径段是符号链接或非目录时照旧拒绝，不会被当作缺失。
    """
    parts = native._workspace.parts(path)
    for depth in range(len(parts) - 1, 0, -1):
        try:
            ancestor = native.observe("/".join(parts[:depth]), access="read", checkpoint=checkpoint)
        except KernelError as error:
            if error.code != "workspace_parent_missing":
                raise
            continue
        if ancestor.kind == "missing":
            rest = parts[depth:]
            return _Observed(
                "missing", (*ancestor.identity, *rest), None, 0, binding=(*ancestor.binding, *rest)
            )
        break
    # 刚才还缺失的祖先此刻已经存在：观察期间有并发创建，不猜测其结果。
    raise KernelError("workspace_changed", "Workspace资源在观察期间变化")


def _observe_request(
    native: _NativeRoot,
    request: WorkspaceResourceRequest,
    requests: Sequence[WorkspaceResourceRequest],
    platform: PlatformKind,
) -> _NativeObservation:
    """第1代快照的逐项观察：原调用形状不变，只在父目录缺失时转入可新建资源的判定。"""
    try:
        return native.observe(request.path, access=request.access)
    except KernelError as error:
        return observe_creatable_resource(native, request, error, requests, platform)


@dataclass(frozen=True, slots=True)
class SecureDirectoryEntry:
    name: str
    kind: Literal["file", "directory", "symlink", "special"]


class SecureWorkspaceReader:
    """复用Workspace句柄链，为只读扩展提供跨平台、无跟随的根能力。"""

    def __init__(self, root: str | Path, *, platform: PlatformKind | None = None) -> None:
        self.platform = platform or _native_platform()
        self._root = _open_native(Path(root), self.platform)
        self.path = self._root.path
        self.root_identity = self._root.root_identity

    def read_file(self, path: str, *, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError("安全读取上限无效")
        normalized = normalize_workspace_path(path, self.platform)
        observed = self._root.observe(normalized, access="read")
        if observed.kind != "file" or observed.content is None:
            raise KernelError("workspace_path_denied", "安全读取目标不是普通文件")
        if observed.size > max_bytes or len(observed.content) > max_bytes:
            raise KernelError("workspace_snapshot_limit", "安全读取目标超过字节上限")
        return bytes(observed.content)

    def list_directory(
        self, path: str = ".", *, max_entries: int = MAX_SNAPSHOT_DIRECTORY_ENTRIES
    ) -> tuple[SecureDirectoryEntry, ...]:
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_SNAPSHOT_DIRECTORY_ENTRIES:
            raise ValueError("目录条目上限无效")
        normalized = normalize_workspace_path(path, self.platform)
        before = self._root.observe(normalized, access="read")
        if before.kind != "directory" or before.entries is None:
            raise KernelError("workspace_path_denied", "安全读取目标不是目录")
        if len(before.entries) > max_entries:
            raise KernelError("workspace_snapshot_limit", "安全目录读取超过条目上限")
        after = self._root.observe(normalized, access="read")
        if (
            before.identity != after.identity
            or before.content != after.content
            or before.size != after.size
            or before.entries != after.entries
        ):
            raise KernelError("workspace_changed", "目录在安全读取期间发生变化")
        return tuple(SecureDirectoryEntry(name=name, kind=kind) for name, kind in before.entries)

    def close(self) -> None:
        self._root.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _snapshot_resource_requests(
    resources: Sequence[WorkspaceResourceRequest], cwd: str, platform: PlatformKind
) -> list[WorkspaceResourceRequest]:
    """保留原请求顺序，补齐cwd/read并在逐叶观察前检查完整资源额度。"""
    requested = list(resources)
    cwd_key = ("workspace", path_comparison_key(cwd, platform), "read")
    keys = {
        (item.location, path_comparison_key(item.path, platform), item.access) for item in requested
    }
    if cwd_key not in keys:
        requested.append(WorkspaceResourceRequest(location="workspace", path=cwd, access="read"))
    if len(requested) > 256:
        raise KernelError("workspace_snapshot_limit", "Workspace快照资源超过上限")
    return requested


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
        requested = _snapshot_resource_requests(resources, normalized_cwd, selected_platform)
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
            observed = _observe_request(native, request, requested, selected_platform)
            total_bytes += len(observed.content or b"")
            if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                raise KernelError("workspace_snapshot_limit", "Workspace快照总量超过上限")
            observations.append(
                WorkspaceResourceObservation(
                    location=request.location,
                    path=path,
                    access=request.access,
                    kind=observed.kind,
                    identity=_digest(observed.binding),
                    content_sha256=(
                        hashlib.sha256(observed.content).hexdigest()
                        if observed.kind == "file" and observed.content is not None
                        else None
                    ),
                    size=bound_size(observed),
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
