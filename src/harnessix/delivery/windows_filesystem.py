"""Windows本地NTFS文件成员端口；复用同一Workspace事务状态机和只观察恢复。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    WorkspaceFileVersion,
    WorkspaceMutation,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.windows_io import WindowsFileOperations, windows_transaction_supported
from harnessix.delivery.windows_metadata import WindowsFileSecurity, check_regular_file
from harnessix.execution.contracts import canonical_digest
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.paths import normalize_workspace_path
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.windows import WindowsWorkspaceRoot, _read_all


@contextmanager
def _parent(
    root: Path, path: str, source: WorkspaceSnapshot | WorkspaceSnapshotV2 | None = None
) -> Iterator[tuple[WindowsWorkspaceRoot, WindowsFileOperations, int, Path, str]]:
    """整个成员操作期间固定所有父段；不跟随Junction，不创建缺失父目录。"""

    if not windows_transaction_supported():
        raise KernelError("delivery_platform_unsupported", "Windows事务要求Windows 11原生端口")
    normalized = normalize_workspace_path(path, "windows")
    if normalized == ".":
        raise KernelError("delivery_path_denied", "Windows事务不能写根目录")
    parent, _, name = normalized.rpartition("/")
    native = WindowsWorkspaceRoot(root)
    handles: list[int] = []
    try:
        if source is not None and (
            source.platform != "windows"
            or source.root_identity != canonical_digest(native.root_identity)
            or source.root_path_digest != canonical_digest(str(native.path).casefold())
        ):
            raise KernelError("delivery_source_changed", "Windows事务Workspace根身份已经变化")
        handles, final = native._open_chain(parent or ".", data=False)
        native._assert_under_root(final)
        if native._information(handles[-1]).attributes & 0x10 == 0:
            raise KernelError("workspace_path_denied", "Windows事务父对象不是目录")
        operations = WindowsFileOperations(native._kernel32)
        operations.require_local_ntfs(handles[-1], native.path.anchor)
        yield native, operations, handles[-1], native.path / Path(parent), name
    finally:
        native._close_all(handles)
        native.close()


def _version(
    native: WindowsWorkspaceRoot, operations: WindowsFileOperations, handle: int | None
) -> WorkspaceFileVersion:
    """同一叶句柄双观测内容与身份；Windows版本模式只表示普通文件0644。"""

    if handle is None:
        return WorkspaceFileVersion(presence="absent", size=0)
    before = native._information(handle)
    check_regular_file(operations.kernel, handle, before)
    operations.rewind(handle)
    body = _read_all(
        operations.kernel, handle, max_bytes=MAX_TRANSACTION_FILE_BYTES, checkpoint=None
    )
    after = native._information(handle)
    size = (after.size_high << 32) | after.size_low
    if native._revision_identity(before) != native._revision_identity(after) or len(body) != size:
        raise KernelError("delivery_source_changed", "Windows事务文件观察期间变化")
    return WorkspaceFileVersion(
        presence="file", sha256=hashlib.sha256(body).hexdigest(), size=len(body), mode=0o644
    )


def windows_workspace_transaction_supported(root: Path) -> bool:
    """无写入地探测根绑定与本地NTFS；不创建探针文件或放宽实际执行检查。"""

    if not windows_transaction_supported():
        return False
    try:
        with _parent(root, ".harnessix-capability-probe"):
            return True
    except (OSError, KernelError, AttributeError):
        return False


def observe_windows_file(
    root: Path, path: str, *, source: WorkspaceSnapshot | WorkspaceSnapshotV2 | None = None
) -> WorkspaceFileVersion:
    """只读查询成员事实，不发布文件，也不为观察创建临时文件。"""

    try:
        with _parent(root, path, source) as (native, operations, _, parent, name):
            handle = operations.open_existing(parent / name)
            try:
                if handle is not None:
                    native._assert_under_root(native._final_path(handle))
                return _version(native, operations, handle)
            finally:
                if handle is not None:
                    operations.kernel.CloseHandle(handle)
    except KernelError:
        raise
    except (OSError, ValueError):
        raise KernelError("delivery_observation_failed", "Windows事务文件观察失败") from None


def _discard_owned_temporary(
    native: WindowsWorkspaceRoot, operations: WindowsFileOperations, handle: int, path: Path
) -> None:
    """仅删除仍位于自有临时名称的句柄；确认丢失后绝不删除已发布目标。"""

    from harnessix.workspace.windows import _api_path

    try:
        if native._final_path(handle).casefold() == _api_path(path).casefold():
            operations.delete(handle)
    except (OSError, KernelError):
        # 临时残留保留为可诊断事实，不尝试路径GC或删除另一个对象。
        pass


def _replace_member(
    native: WindowsWorkspaceRoot,
    operations: WindowsFileOperations,
    parent: Path,
    name: str,
    before_handle: int | None,
    body: bytes,
    mutation: WorkspaceMutation,
    temporary: str,
    checkpoint: Callable[[str], None],
) -> None:
    """先排他写入与Flush，复核before和权限，再以句柄提交唯一名称效果。"""

    path = parent / temporary
    handle = operations.create_temporary(path)
    rename_requested = False
    try:
        native._assert_under_root(native._final_path(handle))
        checkpoint("windows_temporary_created")
        operations.write_and_flush(handle, body)
        checkpoint("windows_temporary_flushed")
        _check_current_target(native, operations, parent / name, before_handle, mutation.before)
        if before_handle is not None:
            WindowsFileSecurity(operations.kernel).require_equal(before_handle, handle)
        rename_requested = True
        operations.rename(handle, name, replace=before_handle is not None)
        checkpoint("windows_namespace_changed")
    finally:
        if not rename_requested:
            _discard_owned_temporary(native, operations, handle, path)
        operations.kernel.CloseHandle(handle)


def _check_current_target(
    native: WindowsWorkspaceRoot,
    operations: WindowsFileOperations,
    path: Path,
    before_handle: int | None,
    before: WorkspaceFileVersion,
) -> None:
    """名称仍须解析到原叶对象；持有旧句柄本身不能证明名称未被替换。"""

    current = operations.open_existing(path)
    try:
        if (current is None) != (before_handle is None):
            raise KernelError("delivery_source_changed", "Windows事务替换前已经漂移")
        if current is not None and before_handle is not None:
            if native._object_identity(native._information(current)) != native._object_identity(
                native._information(before_handle)
            ):
                raise KernelError("delivery_source_changed", "Windows事务目标对象已经变化")
        if _version(native, operations, current) != before:
            raise KernelError("delivery_source_changed", "Windows事务替换前已经漂移")
    finally:
        if current is not None:
            operations.kernel.CloseHandle(current)


def apply_windows_mutation(
    store: SQLiteWorkspaceTransactionStore,
    root: Path,
    transaction_id: UUID,
    index: int,
    mutation: WorkspaceMutation,
    *,
    source: WorkspaceSnapshot | WorkspaceSnapshotV2,
    checkpoint: Callable[[str], None],
) -> None:
    """最多应用一个成员；KernelError由上层按真实before/after结算，不重放未知效果。"""

    if any(version.mode == 0o755 for version in (mutation.before, mutation.after)):
        raise KernelError("delivery_metadata_unsupported", "Windows事务不模拟POSIX可执行模式")
    try:
        with _parent(root, mutation.path, source) as (
            native,
            operations,
            _,
            parent,
            name,
        ):
            handle = operations.open_existing(
                parent / name,
                delete=mutation.after.presence == "absent",
                replace=mutation.after.presence == "file",
            )
            try:
                if handle is not None:
                    native._assert_under_root(native._final_path(handle))
                if _version(native, operations, handle) != mutation.before:
                    raise KernelError("delivery_source_changed", "Windows事务成员提交前已经漂移")
                if mutation.after.presence == "absent":
                    if handle is None:
                        raise KernelError("delivery_source_changed", "Windows事务删除来源已缺失")
                    operations.delete(handle)
                else:
                    if mutation.after.sha256 is None:
                        raise KernelError("delivery_record_invalid", "Windows事务目标版本不完整")
                    _replace_member(
                        native,
                        operations,
                        parent,
                        name,
                        handle,
                        store.blob(mutation.after.sha256),
                        mutation,
                        f".harnessix-{transaction_id.hex}-{index}-{uuid4().hex}.tmp",
                        checkpoint,
                    )
            finally:
                if handle is not None:
                    operations.kernel.CloseHandle(handle)
    except KernelError:
        raise
    except (OSError, ValueError):
        raise KernelError("delivery_storage_unavailable", "Windows事务成员写入失败") from None
