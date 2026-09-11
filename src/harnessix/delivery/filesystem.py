"""Workspace与Git交付：执行并恢复原子Workspace事务。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
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
from harnessix.workspace.contracts import WorkspaceLease
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot import verify_workspace_snapshot

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


def _fault(_: str) -> None:
    """只供崩溃边界测试替换；生产不注入行为。"""


class WorkspaceTransactionRuntime:
    """POSIX普通Workspace的可恢复多文件发布端口。"""

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
    ) -> WorkspaceTransactionRecord:
        """在批准指纹和Workspace Lease仍匹配时发布事务；部分效果转入可恢复状态。"""
        record = self._store.load(transaction_id)
        if approval_fingerprint != record.plan.fingerprint:
            raise KernelError("delivery_approval_mismatch", "Workspace事务批准指纹不匹配")
        if record.plan.source.platform != "posix" or os.name != "posix":
            raise KernelError(
                "delivery_platform_unsupported", "普通Workspace事务当前只支持POSIX安全写端口"
            )
        if record.state == "published":
            return record
        if record.state in {"diverged", "unknown"}:
            raise KernelError("delivery_not_executable", "Workspace事务已处于不可执行终态")
        self._assert_lease(record, lease)
        if record.state == "prepared":
            verify_workspace_snapshot(record.plan.source, root)
            record = self._advance(record, "publishing", 0)
        else:
            record = self.reconcile(transaction_id, root)
            if record.state == "published":
                return record
            if record.state != "interrupted":
                raise KernelError("delivery_not_executable", "Workspace事务不能安全恢复")
            record = self._advance(record, "publishing", record.cursor)
        for index, mutation in enumerate(record.plan.mutations):
            if index < record.cursor:
                continue
            self._assert_lease(record, lease)
            current = _observe(Path(root), mutation.path)
            if current == mutation.after:
                record = self._advance(record, "publishing", index + 1)
                continue
            if current != mutation.before:
                diverged = self._advance(
                    record, "diverged", index, error_code="delivery_source_changed"
                )
                raise KernelError(
                    "delivery_source_changed",
                    f"Workspace事务路径已经漂移：{mutation.path}；状态={diverged.state}",
                )
            try:
                _apply(self._store, Path(root), transaction_id, index, mutation)
                _fault(f"effect_applied:{index}")
            except KernelError:
                reconciled = self.reconcile(transaction_id, root)
                if reconciled.state == "published":
                    return reconciled
                raise
            if _observe(Path(root), mutation.path) != mutation.after:
                unknown = self._advance(
                    record, "unknown", index, error_code="delivery_effect_unknown"
                )
                raise KernelError(
                    "delivery_effect_unknown",
                    f"Workspace事务效果无法证明：{mutation.path}；状态={unknown.state}",
                )
            record = self._advance(record, "publishing", index + 1)
            _fault(f"member_recorded:{index}")
        return self._advance(record, "published", len(record.plan.mutations))

    def reconcile(self, transaction_id: UUID, root: str | Path) -> WorkspaceTransactionRecord:
        """比较前后镜像恢复中断事务；混合或不可证明状态标记为diverged或unknown。"""
        record = self._store.load(transaction_id)
        if record.state in {"published", "diverged", "unknown"}:
            return record
        facts = tuple(_observe(Path(root), item.path) for item in record.plan.mutations)
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
    ) -> WorkspaceTransactionRecord:
        from harnessix.delivery.planner import (
            DesiredWorkspaceFile,
            prepare_workspace_transaction,
        )

        original = self._store.load(transaction_id)
        if original.state != "published":
            raise KernelError("delivery_rollback_invalid", "只有已发布事务可以创建Rollback")
        desired: dict[str, DesiredWorkspaceFile] = {}
        for mutation in original.plan.mutations:
            if mutation.before.presence == "absent":
                desired[mutation.path] = DesiredWorkspaceFile(None)
            else:
                if mutation.before.sha256 is None or mutation.before.mode is None:
                    raise KernelError("delivery_record_invalid", "Rollback来源版本不完整")
                desired[mutation.path] = DesiredWorkspaceFile(
                    self._store.blob(mutation.before.sha256), mutation.before.mode
                )
        prepared = prepare_workspace_transaction(
            root,
            desired,
            request_id=request_id,
            transaction_id=rollback_id or uuid4(),
            now=now or datetime.now(UTC),
            platform=original.plan.source.platform,
        )
        return self._store.save(prepared)

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


def _observe(root: Path, path: str) -> WorkspaceFileVersion:
    try:
        with Workspace(root, path_max_bytes=4096, path_max_parts=128) as workspace:
            with _parent(workspace, path) as (parent, name):
                return _observe_at(parent, name)
    except KernelError:
        raise
    except (OSError, ValueError):
        raise KernelError("delivery_observation_failed", "Workspace事务文件观察失败") from None


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
