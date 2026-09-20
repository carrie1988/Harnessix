"""SQLite维护备份：验证归属、摘要和完整性后创建或原子恢复。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.session.errors import storage_errors

_APPLICATION_ID = 0x4858534B


def resolved_path(value: str | Path) -> Path:
    return Path(value).resolve()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(path: Path) -> str:
    with storage_errors():
        if not path.is_file():
            raise KernelError("maintenance_backup_missing", "维护备份不存在")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as database:
            row = database.execute("PRAGMA quick_check").fetchone()
            app_id = database.execute("PRAGMA application_id").fetchone()
            if row is None or row[0] != "ok" or app_id is None or app_id[0] != _APPLICATION_ID:
                raise KernelError("maintenance_backup_invalid", "维护备份完整性验证失败")
        return file_sha256(path)


def create_or_reuse_backup(
    source_path: Path,
    destination: Path,
    plan_id: UUID,
    expected_plan_sha256: str,
) -> str:
    with storage_errors():
        if destination.exists():
            digest = verify_backup(destination)
            _require_plan(destination, plan_id, expected_plan_sha256)
            return digest
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.backup-{new_id()}.tmp")
        try:
            _copy_database(source_path, temporary)
            _require_plan(temporary, plan_id, expected_plan_sha256)
            with temporary.open("rb+") as created:
                os.fsync(created.fileno())
            os.replace(temporary, destination)
            if os.name == "posix":
                destination.chmod(0o600)
                _fsync_parent(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return file_sha256(destination)


def _copy_database(source_path: Path, destination: Path) -> None:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=5)
    target = sqlite3.connect(destination, timeout=5)
    try:
        source.backup(target)
        row = target.execute("PRAGMA quick_check").fetchone()
        app_id = target.execute("PRAGMA application_id").fetchone()
        if row is None or row[0] != "ok" or app_id is None or app_id[0] != _APPLICATION_ID:
            raise KernelError("maintenance_backup_invalid", "维护备份完整性验证失败")
        target.commit()
    finally:
        target.close()
        source.close()


def _require_plan(path: Path, plan_id: UUID, expected_sha256: str) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as database:
        row = database.execute(
            "SELECT payload_sha256 FROM store_maintenance_plans WHERE plan_id=?",
            (str(plan_id),),
        ).fetchone()
    if row is None or row[0] != expected_sha256:
        raise KernelError("maintenance_backup_changed", "维护备份不属于当前计划")


def restore_database(target: Path, backup: Path) -> str:
    digest = verify_backup(backup)
    temporary = target.with_name(f".{target.name}.restore-{new_id()}.tmp")
    try:
        with storage_errors():
            with sqlite3.connect(target, timeout=5) as database:
                database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copyfile(backup, temporary)
            with temporary.open("rb+") as restored:
                os.fsync(restored.fileno())
            if file_sha256(temporary) != digest:
                raise KernelError("maintenance_backup_invalid", "维护备份复制摘要不一致")
            _replace_database(target, temporary)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


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
