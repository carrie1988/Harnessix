"""Workspace与Git交付：执行并恢复原子Workspace事务。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    FileMode,
    TransactionState,
    WorkspaceFileVersion,
    WorkspaceMutation,
    WorkspaceTransactionRecord,
    transition_transaction_record,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.tools.workspace import Workspace, identity, revision_state
from harnessix.workspace.contracts import WorkspaceLease, WorkspaceSnapshot
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_v2 import verify_workspace_snapshot_v2

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _checkpoint() -> None:
    """旧同步入口不新增整体期限；调用方可注入同一父操作检查。"""


def _fault(_: str) -> None:
    """只供崩溃边界测试替换；生产不注入行为。"""


class WorkspaceTransactionRuntime:
    """共享可恢复发布状态机；文件成员由POSIX或本地NTFS原生端口提交。"""

    def __init__(
        self,
        store: SQLiteWorkspaceTransactionStore,
        leases: WorkspaceLeaseStore,
    ) -> None:
        self._store = store
        self._leases = leases

    def publish(
        self,
        transaction_id: UUID,
        root: str | Path,
        *,
        approval_fingerprint: str,
        lease: WorkspaceLease,
        checkpoint: Callable[[], None] | None = None,
    ) -> WorkspaceTransactionRecord:
        """在批准指纹和Workspace Lease仍匹配时发布事务；部分效果转入可恢复状态。"""
        control = checkpoint or _checkpoint
        while True:
            record = self.publish_next(
                transaction_id,
                root,
                approval_fingerprint=approval_fingerprint,
                lease=lease,
                checkpoint=control,
            )
            if record.state == "published":
                return record

    def publish_next(
        self,
        transaction_id: UUID,
        root: str | Path,
        *,
        approval_fingerprint: str,
        lease: WorkspaceLease,
        checkpoint: Callable[[], None] | None = None,
    ) -> WorkspaceTransactionRecord:
        """最多提交一个有界成员，供异步Owner在成员之间建立取消检查点。"""

        return _publish_next(
            self,
            transaction_id,
            root,
            approval_fingerprint=approval_fingerprint,
            lease=lease,
            checkpoint=checkpoint or _checkpoint,
        )

    def reconcile(self, transaction_id: UUID, root: str | Path) -> WorkspaceTransactionRecord:
        """比较前后镜像恢复中断事务；混合或不可证明状态标记为diverged或unknown。"""
        record = self._store.load(transaction_id)
        if record.state in {"published", "diverged", "unknown"}:
            return record
        facts = _observe_members(record, Path(root))
        after = tuple(
            index
            for index, (fact, item) in enumerate(zip(facts, record.plan.mutations, strict=True))
            if fact == item.after
        )
        before = tuple(
            index
            for index, (fact, item) in enumerate(zip(facts, record.plan.mutations, strict=True))
            if fact == item.before
        )
        if len(after) == len(facts):
            if record.state == "prepared":
                return self._advance(record, "diverged", 0, error_code="delivery_unowned_effect")
            return self._advance(record, "published", len(facts))
        if len(before) + len(after) != len(facts):
            return self._advance(
                record, "diverged", record.cursor, error_code="delivery_source_changed"
            )
        prefix = tuple(range(len(after)))
        if after != prefix or before != tuple(range(len(after), len(facts))):
            return self._advance(
                record, "diverged", record.cursor, error_code="delivery_order_diverged"
            )
        if record.state == "prepared":
            return record
        if record.state == "interrupted" and record.cursor == len(after):
            return record
        return self._advance(record, "interrupted", len(after))

    def build_rollback(
        self,
        transaction_id: UUID,
        root: str | Path,
        *,
        request_id: str,
        rollback_id: UUID | None = None,
        now: datetime | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> WorkspaceTransactionRecord:
        """在原Workspace中规划独立回滚；拒绝根重定位或规划期间的目录置换。"""

        return _build_rollback(
            self,
            transaction_id,
            root,
            request_id=request_id,
            rollback_id=rollback_id,
            now=now,
            checkpoint=checkpoint or _checkpoint,
        )

    def _advance(
        self,
        record: WorkspaceTransactionRecord,
        state: TransactionState,
        cursor: int,
        *,
        error_code: str | None = None,
    ) -> WorkspaceTransactionRecord:
        updated = transition_transaction_record(
            record,
            state=state,
            cursor=cursor,
            now=datetime.now(UTC),
            error_code=error_code,
        )
        self._store.transition(record, updated)
        return updated

    def _assert_lease(self, record: WorkspaceTransactionRecord, lease: WorkspaceLease) -> None:
        if lease.workspace_id != record.plan.source.workspace_id:
            raise KernelError("workspace_lease_lost", "Workspace事务租约不属于当前来源")
        self._leases.assert_current(lease)


def _build_rollback(
    runtime: WorkspaceTransactionRuntime,
    transaction_id: UUID,
    root: str | Path,
    *,
    request_id: str,
    rollback_id: UUID | None,
    now: datetime | None,
    checkpoint: Callable[[], None],
) -> WorkspaceTransactionRecord:
    """绑定原根后构造逆向目标，并在独立Planner捕获之后才保存新事务。"""

    from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction

    checkpoint()
    original = runtime._store.load(transaction_id)
    if original.state != "published":
        raise KernelError("delivery_rollback_invalid", "只有已发布事务可以创建Rollback")
    # 只比较根身份，不拿发布前的资源Revision校验发布后的合法文件内容。
    current_root = capture_workspace_snapshot(root, platform=original.plan.source.platform)
    if current_root.workspace_id != original.plan.source.workspace_id:
        raise KernelError("delivery_source_changed", "Rollback来源Workspace身份已变化")
    desired: dict[str, DesiredWorkspaceFile] = {}
    for mutation in original.plan.mutations:
        checkpoint()
        if mutation.before.presence == "absent":
            desired[mutation.path] = DesiredWorkspaceFile(None)
        else:
            if mutation.before.sha256 is None or mutation.before.mode is None:
                raise KernelError("delivery_record_invalid", "Rollback来源版本不完整")
            desired[mutation.path] = DesiredWorkspaceFile(
                runtime._store.blob(mutation.before.sha256), mutation.before.mode
            )
    checkpoint()
    if isinstance(original.plan.source, WorkspaceSnapshotV2):
        from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2

        prepared = prepare_workspace_transaction_v2(
            root,
            desired,
            request_id=request_id,
            transaction_id=rollback_id or uuid4(),
            now=now or datetime.now(UTC),
            platform=original.plan.source.platform,
            checkpoint=checkpoint,
            write_blob=runtime._store.put_blob,
            read_blob=runtime._store.blob,
        )
    else:
        prepared = prepare_workspace_transaction(
            root,
            desired,
            request_id=request_id,
            transaction_id=rollback_id or uuid4(),
            now=now or datetime.now(UTC),
            platform=original.plan.source.platform,
        )
    # Planner独立捕获完整来源；根在两次捕获之间被替换时不能保存新批准计划。
    if prepared.plan.source.workspace_id != current_root.workspace_id:
        raise KernelError("delivery_source_changed", "Rollback来源Workspace身份已变化")
    return runtime._store.save(prepared)


@contextmanager
def _parent(workspace: Workspace, path: str) -> Iterator[tuple[int, str]]:
    parts = workspace.parts(path)
    if not parts:
        raise KernelError("delivery_path_denied", "Workspace事务不能写根目录")
    with ExitStack() as stack:
        descriptor = workspace._current_root()
        stack.callback(os.close, descriptor)
        root_device = os.fstat(descriptor).st_dev
        links: list[tuple[int, str, int]] = []
        for component in parts[:-1]:
            parent = descriptor
            before = os.stat(component, dir_fd=parent, follow_symlinks=False)
            Workspace._check_type(before, directory=True)
            descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            stack.callback(os.close, descriptor)
            after = os.fstat(descriptor)
            Workspace._check_type(after, directory=True)
            if after.st_dev != root_device or identity(before) != identity(after):
                raise KernelError("delivery_source_changed", "Workspace事务父目录已经变化")
            links.append((parent, component, descriptor))
        yield descriptor, parts[-1]
        os.close(workspace._current_root())
        for parent, component, child in links:
            current = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if identity(current) != identity(os.fstat(child)):
                raise KernelError("delivery_source_changed", "Workspace事务父目录已经变化")


def _observe(
    root: Path, path: str, *, source: WorkspaceSnapshot | WorkspaceSnapshotV2 | None = None
) -> WorkspaceFileVersion:
    if os.name == "nt":
        from harnessix.delivery.windows_filesystem import observe_windows_file

        return observe_windows_file(root, path, source=source)
    try:
        with Workspace(root, path_max_bytes=4096, path_max_parts=128) as workspace:
            with _parent(workspace, path) as (parent, name):
                return _observe_at(parent, name)
    except KernelError:
        raise
    except (OSError, ValueError):
        raise KernelError("delivery_observation_failed", "Workspace事务文件观察失败") from None


def _observe_members(
    record: WorkspaceTransactionRecord, root: Path
) -> tuple[WorkspaceFileVersion, ...]:
    """只观察原平台的全部成员；Windows逐成员绑定原Root，不执行恢复写入。"""

    _assert_platform(record.plan.source)
    return tuple(
        _observe(root, item.path, source=record.plan.source) for item in record.plan.mutations
    )


def _observe_at(parent: int, name: str) -> WorkspaceFileVersion:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return WorkspaceFileVersion(presence="absent", size=0)
    Workspace._check_type(before, directory=False)
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        if identity(before) != identity(opened):
            raise KernelError("delivery_source_changed", "Workspace事务文件对象已经变化")
        body = bytearray()
        while len(body) <= 8 * 1024 * 1024:
            chunk = os.read(descriptor, min(65_536, 8 * 1024 * 1024 + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > 8 * 1024 * 1024:
            raise KernelError("delivery_plan_limit", "Workspace事务文件超过上限")
        after = os.fstat(descriptor)
        if revision_state(opened) != revision_state(after) or len(body) != after.st_size:
            raise KernelError("delivery_source_changed", "Workspace事务文件读取期间变化")
        actual_mode = stat.S_IMODE(after.st_mode)
        if actual_mode == 0o644:
            mode: FileMode = 0o644
        elif actual_mode == 0o755:
            mode = 0o755
        else:
            raise KernelError(
                "delivery_metadata_unsupported", "Workspace事务只支持0644或0755普通文件"
            )
        return WorkspaceFileVersion(
            presence="file",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            mode=mode,
        )
    finally:
        os.close(descriptor)


def _apply(
    store: SQLiteWorkspaceTransactionStore,
    root: Path,
    transaction_id: UUID,
    index: int,
    mutation: WorkspaceMutation,
) -> None:
    if os.name == "nt":
        from harnessix.delivery.windows_filesystem import apply_windows_mutation

        apply_windows_mutation(
            store,
            root,
            transaction_id,
            index,
            mutation,
            source=store.load(transaction_id).plan.source,
            checkpoint=_fault,
        )
        return
    with Workspace(root, path_max_bytes=4096, path_max_parts=128) as workspace:
        with _parent(workspace, mutation.path) as (parent, name):
            if _observe_at(parent, name) != mutation.before:
                raise KernelError("delivery_source_changed", "Workspace事务成员提交前已经漂移")
            if mutation.after.presence == "absent":
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
                return
            if mutation.after.sha256 is None or mutation.after.mode is None:
                raise KernelError("delivery_record_invalid", "Workspace事务目标版本不完整")
            body = store.blob(mutation.after.sha256)
            temporary = f".harnessix-{transaction_id.hex}-{index}-{uuid4().hex}.tmp"
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent,
                )
                offset = 0
                while offset < len(body):
                    written = os.write(descriptor, body[offset : offset + 65_536])
                    if written <= 0:
                        raise OSError
                    offset += written
                os.fchmod(descriptor, mutation.after.mode)
                os.fsync(descriptor)
                if _observe_at(parent, name) != mutation.before:
                    raise KernelError("delivery_source_changed", "Workspace事务成员替换前已经漂移")
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            except KernelError:
                raise
            except OSError:
                raise KernelError(
                    "delivery_storage_unavailable", "Workspace事务成员写入失败"
                ) from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass


def _publish_next(
    runtime: WorkspaceTransactionRuntime,
    transaction_id: UUID,
    root: str | Path,
    *,
    approval_fingerprint: str,
    lease: WorkspaceLease,
    checkpoint: Callable[[], None],
) -> WorkspaceTransactionRecord:
    """复核执行资格并最多提交一个Workspace事务成员。"""

    record = _prepare_publication(
        runtime,
        transaction_id,
        root,
        approval_fingerprint=approval_fingerprint,
        lease=lease,
        checkpoint=checkpoint,
    )
    if record.state == "published":
        return record
    index = record.cursor
    mutation = record.plan.mutations[index]
    runtime._assert_lease(record, lease)
    current = _observe(Path(root), mutation.path, source=record.plan.source)
    if current == mutation.after:
        record = runtime._advance(record, "publishing", index + 1)
    elif current != mutation.before:
        diverged = runtime._advance(record, "diverged", index, error_code="delivery_source_changed")
        raise KernelError(
            "delivery_source_changed",
            f"Workspace事务路径已经漂移：{mutation.path}；状态={diverged.state}",
        )
    else:
        try:
            _apply(runtime._store, Path(root), transaction_id, index, mutation)
            _fault(f"effect_applied:{index}")
        except KernelError:
            reconciled = runtime.reconcile(transaction_id, root)
            if reconciled.state == "published":
                return reconciled
            raise
        if _observe(Path(root), mutation.path, source=record.plan.source) != mutation.after:
            unknown = runtime._advance(
                record, "unknown", index, error_code="delivery_effect_unknown"
            )
            raise KernelError(
                "delivery_effect_unknown",
                f"Workspace事务效果无法证明：{mutation.path}；状态={unknown.state}",
            )
        record = runtime._advance(record, "publishing", index + 1)
        _fault(f"member_recorded:{index}")
    if record.cursor == len(record.plan.mutations):
        return runtime._advance(record, "published", record.cursor)
    return record


def _prepare_publication(
    runtime: WorkspaceTransactionRuntime,
    transaction_id: UUID,
    root: str | Path,
    *,
    approval_fingerprint: str,
    lease: WorkspaceLease,
    checkpoint: Callable[[], None],
) -> WorkspaceTransactionRecord:
    """复核批准、平台、租约和可恢复状态，但不提交文件成员。"""

    checkpoint()
    record = runtime._store.load(transaction_id)
    checkpoint()
    if approval_fingerprint != record.plan.fingerprint:
        raise KernelError("delivery_approval_mismatch", "Workspace事务批准指纹不匹配")
    _assert_platform(record.plan.source)
    if record.state == "published":
        return record
    if record.state in {"diverged", "unknown"}:
        raise KernelError("delivery_not_executable", "Workspace事务已处于不可执行终态")
    runtime._assert_lease(record, lease)
    if record.state == "prepared":
        if isinstance(record.plan.source, WorkspaceSnapshotV2):
            verify_workspace_snapshot_v2(
                record.plan.source, root, checkpoint=checkpoint, read_blob=runtime._store.blob
            )
        else:
            verify_workspace_snapshot(record.plan.source, root)
        record = runtime._advance(record, "publishing", 0)
    else:
        record = runtime.reconcile(transaction_id, root)
        if record.state == "published":
            return record
        if record.state != "interrupted":
            raise KernelError("delivery_not_executable", "Workspace事务不能安全恢复")
        record = runtime._advance(record, "publishing", record.cursor)
    if record.cursor == len(record.plan.mutations):
        return runtime._advance(record, "published", record.cursor)
    return record


def _assert_platform(source: WorkspaceSnapshot | WorkspaceSnapshotV2) -> None:
    """跨宿主历史不能落入另一平台的观察/执行端口，也不能据此追认效果。"""

    native_platform = "windows" if os.name == "nt" else "posix"
    if os.name not in {"posix", "nt"} or source.platform != native_platform:
        raise KernelError(
            "delivery_platform_unsupported", "Workspace事务来源平台与本机安全写端口不一致"
        )
