"""停机完整产品备份与只读验真；根外本机回执授权原备份，不接收自签包替代原来源。"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.product_config.state_backup_contracts import (
    DATABASES,
    MAX_BACKUP_MANIFEST_BYTES,
    MAX_STATE_BYTES,
    MAX_STATE_FILE_BYTES,
    PROCESS_DATABASE,
    ProductStateBackupManifest,
    ProductStateBackupReceipt,
    ProductStateFile,
    ephemeral_state_file,
    state_directory_allowed,
    state_file_kind,
)
from harnessix.product_config.state_backup_files import (
    PrivateStateTree,
    absolute_address,
    copy_file,
    create_private_tree,
    file_digest,
    file_revisions,
    publish_tree,
    read_small,
    write_new,
)
from harnessix.product_config.state_backup_validation import (
    original_key,
    require_backup_outside_workspaces,
    validate_product_state,
)
from harnessix.product_config.state_owner import (
    ProductStateOwner,
    product_state_owner,
    state_owner_anchor,
)
from harnessix.session.maintenance_io import MaintenanceIOControl, run_maintenance_io


@contextmanager
def _backup_errors() -> Iterator[None]:
    try:
        yield
    except KernelError:
        raise
    except (OSError, sqlite3.Error, ValidationError, ValueError, RuntimeError):
        raise KernelError(
            "product_backup_invalid", "产品备份操作失败，未获得完整可信备份"
        ) from None


def _paths(source: PrivateStateTree, control: MaintenanceIOControl) -> tuple[str, ...]:
    managed = []
    total = 0
    for path in source.files(
        control, directories=state_directory_allowed, transient=ephemeral_state_file
    ):
        if ephemeral_state_file(path):
            continue
        state_file_kind(path)
        with source.open_file(path) as descriptor:
            size = os.fstat(descriptor).st_size
        total += size
        if size > MAX_STATE_FILE_BYTES or total > MAX_STATE_BYTES:
            raise KernelError("product_backup_limit", "产品备份超过容量上限")
        managed.append(path)
    if not set(DATABASES) <= set(managed):
        raise KernelError("product_backup_layout_invalid", "产品备份缺少完整受管数据库")
    return tuple(managed)


@contextmanager
def _quiet_databases(
    tree: PrivateStateTree,
    paths: tuple[str, ...],
    control: MaintenanceIOControl,
    *,
    tolerate_corrupt: bool = False,
) -> Iterator[None]:
    """同时持有所有库的保留写锁；不提交业务变更、不推进Fence或清理UNKNOWN。"""
    with ExitStack() as resources:
        for lock in ("sessions.db.runtime.lock", "action-audit.db.runtime.lock"):
            if not os.path.lexists(tree.path / lock):
                write_new(tree, lock, b"\0")
            descriptor = resources.enter_context(tree.open_file(lock, writable=True))
            os.lseek(descriptor, 0, os.SEEK_SET)
            acquire_exclusive_file_lock(descriptor)
        for path in paths:
            if path not in (*DATABASES, PROCESS_DATABASE):
                continue
            control.checkpoint()
            database = resources.enter_context(
                closing(
                    sqlite3.connect(
                        (tree.path / path).as_uri() + "?mode=rw",
                        uri=True,
                        timeout=0.1,
                    )
                )
            )
            database.set_progress_handler(control.interrupt, 1000)
            try:
                database.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", 0) & 0xFF in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise KernelError(
                        "product_backup_not_quiet", "产品数据库仍有活跃写入者"
                    ) from None
                raise
            except sqlite3.DatabaseError as error:
                if tolerate_corrupt and getattr(error, "sqlite_errorcode", 0) & 0xFF in {
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                }:
                    continue
                raise
        yield
        control.checkpoint()


def _copy_database(
    source: PrivateStateTree,
    target: PrivateStateTree,
    path: str,
    control: MaintenanceIOControl,
) -> None:
    with source.open_file(path):
        with target.open_file(path, create=True) as descriptor:
            os.fsync(descriptor)
        with (
            closing(
                sqlite3.connect((source.path / path).as_uri() + "?mode=ro", uri=True, timeout=0.1)
            ) as reader,
            closing(sqlite3.connect(target.path / path, timeout=0.1)) as writer,
        ):
            page_size = reader.execute("PRAGMA page_size").fetchone()[0]

            def progress(status: int, remaining: int, total: int) -> None:
                control.checkpoint()
                if total * page_size > MAX_STATE_FILE_BYTES:
                    raise KernelError("product_backup_limit", "产品备份数据库超过容量上限")

            reader.backup(writer, pages=128, progress=progress, sleep=0.01)
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.commit()
        # Windows的FlushFileBuffers需要可写Handle；只读CRT FD上的fsync会返回EBADF。
        # SQLite读写连接已关闭，再独占原私有文件，不扩大读端口写权限。
        with target.open_file(path, writable=os.name == "nt") as descriptor:
            os.fsync(descriptor)


def _manifest(
    target: PrivateStateTree,
    paths: tuple[str, ...],
    control: MaintenanceIOControl,
) -> ProductStateBackupManifest:
    store_id, key_id = validate_product_state(target, paths, control)
    entries = []
    for path in paths:
        size, digest = file_digest(target, path, control)
        entries.append(
            ProductStateFile(path=path, kind=state_file_kind(path), size_bytes=size, sha256=digest)
        )
    return ProductStateBackupManifest(
        backup_id=uuid4(),
        created_at=utc_now(),
        platform=cast(Literal["posix", "nt"], os.name),
        store_id=store_id,
        key_id=key_id,
        files=tuple(entries),
    )


def _destination(owner: ProductStateOwner, destination: Path) -> Path:
    target = absolute_address(destination)
    root, anchor = owner.state_root, state_owner_anchor(owner.state_root)
    if any(target.is_relative_to(path) or path.is_relative_to(target) for path in (root, anchor)):
        raise KernelError("product_backup_overlap", "备份目录不能与产品状态或信任锚点重叠")
    if os.path.lexists(target):
        raise KernelError("product_backup_exists", "备份目标已经存在，禁止覆盖")
    return target


def _backup(
    owner: ProductStateOwner,
    destination: Path,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> ProductStateBackupManifest:
    owner.require(owner.state_root)
    target = _destination(owner, destination)
    temporary = target.with_name(f".{target.name}.backup-{uuid4()}")
    create_private_tree(temporary)
    temporary_identity = temporary.stat().st_dev, temporary.stat().st_ino
    receipt_name: str | None = None
    published = False
    try:
        with PrivateStateTree(temporary) as bundle, PrivateStateTree(owner.state_root) as source:
            bundle.directory("state")
            with PrivateStateTree(temporary / "state") as copied:
                copied.directory("workspace-transactions/blobs")
                paths = _paths(source, control)
                with _quiet_databases(source, paths, control):
                    require_backup_outside_workspaces(source, target, control)
                    revisions = file_revisions(source, paths, control)
                    fault("backup.after_quiet")
                    for path in paths:
                        control.checkpoint()
                        owner.require(owner.state_root)
                        if state_file_kind(path) == "database":
                            _copy_database(source, copied, path, control)
                        else:
                            copy_file(source, copied, path, control)
                        fault("backup.after_copy:" + path)
                    if paths != _paths(source, control) or paths != copied.files(
                        control, directories=state_directory_allowed
                    ):
                        raise KernelError(
                            "product_backup_changed", "产品状态清单在备份期间发生变化"
                        )
                    if revisions != file_revisions(source, paths, control):
                        raise KernelError("product_backup_changed", "产品原文件在备份期间发生变化")
                    manifest = _manifest(copied, paths, control)
                body = manifest.model_dump_json(warnings="error").encode()
                if len(body) > MAX_BACKUP_MANIFEST_BYTES:
                    raise KernelError("product_backup_limit", "产品备份清单超过容量上限")
                write_new(bundle, "manifest.json", body)
        control.checkpoint()
        owner.require(owner.state_root)
        receipt = ProductStateBackupReceipt(
            backup_id=manifest.backup_id,
            manifest_sha256=hashlib.sha256(body).hexdigest(),
            store_id=manifest.store_id,
            key_id=manifest.key_id,
        )
        with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
            name = f"backup-{manifest.backup_id}.json"
            write_new(anchor, name, receipt.model_dump_json(warnings="error").encode())
            receipt_name = name
        # 已完整验真的本机回执先耐久，再不可覆盖发布目录；这段提交不再中途取消。
        fault("backup.after_receipt")
        publish_tree(temporary, target)
        published = True
        fault("backup.after_publish")
        return manifest
    finally:
        if not published:
            _cleanup_unpublished_candidate(owner, temporary, temporary_identity, receipt_name)


def _cleanup_unpublished_candidate(
    owner: ProductStateOwner,
    temporary: Path,
    identity: tuple[int, int],
    receipt_name: str | None,
) -> None:
    """只有原候选仍在原地址时才能清理；发布确认丢失不得反向撤销原可信回执。"""
    try:
        info = temporary.lstat()
    except FileNotFoundError:
        # Rename可能已提交但fsync或确认返回失败，保留回执供完整只读验真。
        return
    if (info.st_dev, info.st_ino) != identity:
        return
    if receipt_name is not None:
        (state_owner_anchor(owner.state_root) / receipt_name).unlink(missing_ok=True)
    shutil.rmtree(temporary)


def _verify_current_key(owner: ProductStateOwner, candidate: PrivateStateTree) -> None:
    if not owner.state_root.exists():
        return
    with PrivateStateTree(owner.state_root) as current:
        with ExitStack() as resources:
            first = original_key(current)
            resources.callback(first.close)
            second = original_key(candidate)
            resources.callback(second.close)
            if (
                first.store_id != second.store_id
                or first.key_id != second.key_id
                or not hmac.compare_digest(first.key, second.key)
            ):
                raise KernelError("product_backup_key_mismatch", "备份不属于当前产品原Key身份")


def _require_budget(budget_seconds: float) -> None:
    if not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 300:
        raise KernelError("product_backup_budget_invalid", "产品备份期限必须在零至300秒之间")


def trusted_backup_manifest(owner: ProductStateOwner, body: bytes) -> ProductStateBackupManifest:
    """原Manifest字节必须匹配根外回执；Journal不能用重新序列化或自带Key冒充来源。"""
    manifest = ProductStateBackupManifest.model_validate_json(body)
    if manifest.platform != os.name:
        raise KernelError("product_backup_platform_mismatch", "备份仅支持原平台同机同用户验真")
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        receipt = ProductStateBackupReceipt.model_validate_json(
            read_small(anchor, f"backup-{manifest.backup_id}.json", 4096)
        )
    if receipt != ProductStateBackupReceipt(
        backup_id=manifest.backup_id,
        manifest_sha256=hashlib.sha256(body).hexdigest(),
        store_id=manifest.store_id,
        key_id=manifest.key_id,
    ):
        raise KernelError("product_backup_untrusted", "备份缺少原状态根外的匹配本机回执")
    return manifest


def verify_state_snapshot(
    state: PrivateStateTree, manifest: ProductStateBackupManifest, control: MaintenanceIOControl
) -> None:
    """备份与恢复共用原Schema/MAC/引用核验；副本必须保持原文件集合及版本。"""
    paths = tuple(entry.path for entry in manifest.files)
    if _paths(state, control) != paths:
        raise KernelError("product_backup_layout_invalid", "产品状态与备份清单不匹配")
    revisions = file_revisions(state, paths, control)
    for entry in manifest.files:
        control.checkpoint()
        if file_digest(state, entry.path, control) != (entry.size_bytes, entry.sha256):
            raise KernelError("product_backup_changed", "产品备份原文件与可信清单不匹配")
    if validate_product_state(state, paths, control) != (manifest.store_id, manifest.key_id):
        raise KernelError("product_backup_key_mismatch", "备份Key与原Store身份不匹配")
    if revisions != file_revisions(state, paths, control):
        raise KernelError("product_backup_changed", "状态在只读验真期间发生变化")


def _verify(
    owner: ProductStateOwner,
    destination: Path,
    control: MaintenanceIOControl,
) -> ProductStateBackupManifest:
    owner.require(owner.state_root)
    target = absolute_address(destination)
    with PrivateStateTree(target) as bundle:
        manifest = trusted_backup_manifest(
            owner, read_small(bundle, "manifest.json", MAX_BACKUP_MANIFEST_BYTES)
        )
        expected = ("manifest.json", *("state/" + entry.path for entry in manifest.files))
        actual = bundle.files(
            control,
            directories=lambda path: (
                path == "state" or (path.startswith("state/") and state_directory_allowed(path[6:]))
            ),
        )
        if actual != tuple(sorted(expected)):
            raise KernelError("product_backup_layout_invalid", "备份目录与受管清单不匹配")
        with PrivateStateTree(target / "state") as state:
            verify_state_snapshot(state, manifest, control)
            _verify_current_key(owner, state)
    owner.require(owner.state_root)
    control.checkpoint()
    return manifest


async def backup_product_state(
    state_root: Path,
    destination: Path,
    *,
    budget_seconds: float = 120.0,
    fault: Callable[[str], None] | None = None,
) -> ProductStateBackupManifest:
    """停机捕获完整产品事实；只有原工作线程结算后才能释放根外Owner。"""
    _require_budget(budget_seconds)
    with _backup_errors(), product_state_owner(state_root) as owner:
        owner.require_ready(owner.state_root)
        return await run_maintenance_io(
            lambda control: _controlled(
                lambda: _backup(owner, destination, control, fault or (lambda _: None)), control
            ),
            budget_seconds=budget_seconds,
        )


async def verify_product_backup(
    state_root: Path,
    destination: Path,
    *,
    budget_seconds: float = 120.0,
) -> ProductStateBackupManifest:
    """原根丢失时仍从独立本机回执验真；不修改备份、当前状态或原证明。"""
    _require_budget(budget_seconds)
    with _backup_errors(), product_state_owner(state_root) as owner:
        owner.require_ready(owner.state_root)
        return await run_maintenance_io(
            lambda control: _controlled(lambda: _verify(owner, destination, control), control),
            budget_seconds=budget_seconds,
        )


def _controlled[T](operation: Callable[[], T], control: MaintenanceIOControl) -> T:
    """SQLite进度回调只能返回中断码；退出时恢复原取消或期限原因。"""
    try:
        return operation()
    except sqlite3.Error:
        control.checkpoint()
        raise
