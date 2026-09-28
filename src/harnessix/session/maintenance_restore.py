"""恢复候选副本的Session预检；不注册第二Runtime，不接触现行数据库。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.session.capacity import capacity_report
from harnessix.session.maintenance_backup import resolved_path, restore_database
from harnessix.session.maintenance_contracts import StoreRestoreReport
from harnessix.session.maintenance_io import (
    MaintenanceIOControl,
    require_legacy_maintenance,
    run_maintenance_io,
)
from harnessix.session.sqlite import SQLiteSessionStore


async def restore_session(
    session: SQLiteSessionStore,
    backup_path: str | Path,
    *,
    owner: object,
    fault: Callable[[str], None],
) -> StoreRestoreReport:
    """静默宿主恢复旧式库；当前Store及候选副本先验证，工作线程收敛后才返回。"""
    require_legacy_maintenance(session._publication)
    async with session._connection():
        pass
    backup = await run_maintenance_io(lambda _: resolved_path(backup_path))
    if backup == session.path:
        raise KernelError("maintenance_invalid", "恢复源不能是当前Session数据库")

    def require_owner() -> None:
        if session._runtime_owner_token is not owner:
            raise KernelError("maintenance_runtime_required", "Store恢复期间宿主已关闭")

    digest_value = await run_maintenance_io(
        lambda control: restore_database(
            session.path,
            backup,
            validate=validate_restore_candidate,
            require_owner=require_owner,
            control=control,
            fault=fault,
        )
    )
    require_owner()
    await session.initialize()
    async with session._connection() as database:
        await database.execute("BEGIN")
        capacity = await capacity_report(database, session.path)
    return StoreRestoreReport(
        restored_at=datetime.now(UTC), backup_sha256=digest_value, capacity=capacity
    )


def validate_restore_candidate(path: Path, control: MaintenanceIOControl) -> None:
    """只在受管工作线程内运行独立事件循环；认证备份不得降级成无Key库。"""
    asyncio.run(_validate_candidate(path, control))


async def _validate_candidate(path: Path, control: MaintenanceIOControl) -> None:
    candidate = SQLiteSessionStore(path)
    control.checkpoint()
    # initialize先验Migration历史和Store身份，所有前向迁移只落在自有候选副本。
    await candidate.initialize()
    control.checkpoint()
    await candidate.recovery_threads()
    control.checkpoint()
    async with candidate._connection() as database:
        await database.execute("BEGIN")
        await capacity_report(database, path)
    control.checkpoint()
