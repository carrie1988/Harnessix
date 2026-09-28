"""SQLite维护备份：验证归属、摘要和完整性后创建或原子恢复。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.session.errors import storage_errors
from harnessix.session.maintenance_io import MaintenanceIOControl

_APPLICATION_ID = 0x4858534B
MAX_BACKUP_BYTES = 256 * 1024 * 1024


def resolved_path(value: str | Path) -> Path:
    return Path(value).resolve()


def file_sha256(path: Path, *, control: MaintenanceIOControl | None = None) -> str:
    control = control or MaintenanceIOControl()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        if os.fstat(source.fileno()).st_size > MAX_BACKUP_BYTES:
            raise KernelError("maintenance_backup_limit", "维护备份超过文件大小上限")
        total = 0
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            control.checkpoint()
            total += len(chunk)
            if total > MAX_BACKUP_BYTES:
                raise KernelError("maintenance_backup_limit", "维护备份超过文件大小上限")
            digest.update(chunk)
        control.checkpoint()
    return digest.hexdigest()


def verify_backup(path: Path, *, control: MaintenanceIOControl | None = None) -> str:
    control = control or MaintenanceIOControl()
    with storage_errors():
        if not path.is_file():
            raise KernelError("maintenance_backup_missing", "维护备份不存在")
        digest = file_sha256(path, control=control)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
            database.set_progress_handler(control.interrupt, 1000)
            try:
                row = database.execute("PRAGMA quick_check").fetchone()
                app_id = database.execute("PRAGMA application_id").fetchone()
            except sqlite3.DatabaseError:
                control.checkpoint()
                raise
            if row is None or row[0] != "ok" or app_id is None or app_id[0] != _APPLICATION_ID:
                raise KernelError("maintenance_backup_invalid", "维护备份完整性验证失败")
        control.checkpoint()
        return digest


def create_or_reuse_backup(
    source_path: Path,
    destination: Path,
    plan_id: UUID,
    expected_plan_sha256: str,
    *,
    control: MaintenanceIOControl | None = None,
) -> str:
    control = control or MaintenanceIOControl()
    with storage_errors():
        control.checkpoint()
        if destination.exists():
            digest = verify_backup(destination, control=control)
            _require_plan(destination, plan_id, expected_plan_sha256)
            return digest
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.backup-{new_id()}.tmp")
        try:
            _copy_database(source_path, temporary, control)
            _require_plan(temporary, plan_id, expected_plan_sha256)
            with temporary.open("rb+") as created:
                os.fsync(created.fileno())
            control.checkpoint()
            # 不覆盖其他调用者已发布的备份；竞争失败只清理自有临时文件。
            os.link(temporary, destination)
            if os.name == "posix":
                destination.chmod(0o600)
                _fsync_parent(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return file_sha256(destination, control=control)


def _copy_database(source_path: Path, destination: Path, control: MaintenanceIOControl) -> None:
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with (
        closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as source,
        closing(sqlite3.connect(destination, timeout=5)) as target,
    ):
        try:
            row = source.execute("PRAGMA page_size").fetchone()
            assert row is not None
            page_size = row[0]

            def progress(status: int, remaining: int, total: int) -> None:
                control.checkpoint()
                if total * page_size > MAX_BACKUP_BYTES:
                    raise KernelError("maintenance_backup_limit", "维护备份超过文件大小上限")

            source.backup(target, pages=128, progress=progress, sleep=0.05)
            target.set_progress_handler(control.interrupt, 1000)
            row = target.execute("PRAGMA quick_check").fetchone()
            app_id = target.execute("PRAGMA application_id").fetchone()
            if row is None or row[0] != "ok" or app_id is None or app_id[0] != _APPLICATION_ID:
                raise KernelError("maintenance_backup_invalid", "维护备份完整性验证失败")
            target.commit()
        except sqlite3.DatabaseError:
            control.checkpoint()
            raise


def _require_plan(path: Path, plan_id: UUID, expected_sha256: str) -> None:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
        row = database.execute(
            "SELECT payload_sha256 FROM store_maintenance_plans WHERE plan_id=?",
            (str(plan_id),),
        ).fetchone()
    if row is None or row[0] != expected_sha256:
        raise KernelError("maintenance_backup_changed", "维护备份不属于当前计划")


def restore_database(
    target: Path,
    backup: Path,
    *,
    validate: Callable[[Path, MaintenanceIOControl], None],
    require_owner: Callable[[], None],
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> str:
    digest = verify_backup(backup, control=control)
    temporary = target.with_name(f".{target.name}.restore-{new_id()}.tmp")
    try:
        with storage_errors():
            _copy_file(backup, temporary, control)
            with temporary.open("rb+") as restored:
                os.fsync(restored.fileno())
            if file_sha256(temporary, control=control) != digest:
                raise KernelError("maintenance_backup_invalid", "维护备份复制摘要不一致")
            # 只迁移及校验自有副本；任何拒绝都发生在当前数据库及WAL改动之前。
            validate(temporary, control)
            fault("maintenance.restore_after_validation")
            control.checkpoint()
            require_owner()
            _checkpoint_target(target, control)
            control.checkpoint()
            require_owner()
            _replace_database(target, temporary)
            fault("maintenance.restore_after_publish")
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
    return digest


def _copy_file(source: Path, target: Path, control: MaintenanceIOControl) -> None:
    total = 0
    with source.open("rb") as original, target.open("xb") as copied:
        if os.name == "posix":
            os.fchmod(copied.fileno(), 0o600)
        for chunk in iter(lambda: original.read(1024 * 1024), b""):
            control.checkpoint()
            total += len(chunk)
            if total > MAX_BACKUP_BYTES:
                raise KernelError("maintenance_backup_limit", "维护备份超过文件大小上限")
            copied.write(chunk)
        control.checkpoint()


def _checkpoint_target(target: Path, control: MaintenanceIOControl) -> None:
    with closing(sqlite3.connect(target, timeout=5)) as database:
        database.set_progress_handler(control.interrupt, 1000)
        try:
            result = database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.DatabaseError:
            control.checkpoint()
            raise
        if result is None or result[0] != 0:
            raise KernelError("maintenance_restore_busy", "Store恢复需要静默维护窗口")


def _replace_database(target: Path, temporary: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    os.replace(temporary, target)
    if os.name == "posix":
        target.chmod(0o600)
        _fsync_parent(target.parent)


def _fsync_parent(parent: Path) -> None:
    descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
