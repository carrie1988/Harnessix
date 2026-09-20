"""Session共库维护：持久化Dry Run计划、分页清理、备份与显式回滚。"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.session.capacity import capacity_report, load_thread_facts
from harnessix.session.errors import storage_errors
from harnessix.session.maintenance_backup import (
    create_or_reuse_backup,
    resolved_path,
    restore_database,
    verify_backup,
)
from harnessix.session.maintenance_contracts import (
    MaintenanceExecutionReport,
    MaintenanceItemKind,
    MaintenancePlan,
    MaintenanceProgress,
    RetentionPolicy,
    StoreCapacityReport,
    StoreRestoreReport,
)
from harnessix.session.maintenance_execution import run_batches
from harnessix.session.maintenance_planning import build_candidates
from harnessix.session.maintenance_records import (
    PlanItem,
    artifact_rows,
    candidate_set_sha256,
    digest,
    load_plan_items,
    load_progress,
    request_rows,
    save_plan,
)
from harnessix.session.sqlite import SQLiteSessionStore


async def _current_plan(
    session: SQLiteSessionStore, plan_id: UUID
) -> tuple[MaintenancePlan, list[PlanItem], MaintenanceProgress]:
    async with session._connection() as database:
        await database.execute("BEGIN")
        plan, items = await load_plan_items(database, plan_id)
        progress = await load_progress(database, plan_id, len(items))
        return plan, items, progress


async def _start_execution(
    session: SQLiteSessionStore,
    plan: MaintenancePlan,
    items: list[PlanItem],
    backup: Path,
    owner: object,
    fault: Callable[[str], None],
) -> None:
    plan_sha256 = digest(plan.model_dump_json(warnings="error"))
    backup_sha256 = await asyncio.to_thread(
        create_or_reuse_backup,
        session.path,
        backup,
        plan.plan_id,
        plan_sha256,
    )
    fault("maintenance.after_backup_created")
    async with session._connection() as database:
        await database.execute("BEGIN IMMEDIATE")
        current = await load_progress(database, plan.plan_id, len(items))
        if current.state != "planned":
            raise KernelError("maintenance_state_conflict", "维护计划已由其他执行者启动")
        if await _schema_version(database) != plan.schema_version:
            raise KernelError("maintenance_schema_changed", "维护计划创建后Schema已变化")
        now = datetime.now(UTC).isoformat()
        updated = await database.execute(
            "UPDATE store_maintenance_progress SET state='running',backup_sha256=?,"
            "started_at=?,updated_at=? WHERE plan_id=? AND state='planned'",
            (backup_sha256, now, now, str(plan.plan_id)),
        )
        if updated.rowcount != 1:
            raise KernelError("maintenance_state_conflict", "维护计划启动CAS失败")
        if session._runtime_owner_token is not owner:
            raise KernelError("maintenance_runtime_required", "Store维护启动期间宿主已关闭")
        await database.commit()
    fault("maintenance.after_backup_commit")


async def _schema_version(database: aiosqlite.Connection) -> int:
    cursor = await database.execute("SELECT COALESCE(MAX(version),0) FROM agent_migrations")
    row = await cursor.fetchone()
    assert row is not None
    version = row[0]
    if type(version) is not int:
        raise KernelError("invalid_migration", "Session Migration版本无效")
    return version


async def _execution_result(
    session: SQLiteSessionStore, plan_id: UUID
) -> MaintenanceExecutionReport:
    async with session._connection() as database:
        await database.execute("BEGIN")
        plan, items = await load_plan_items(database, plan_id)
        progress = await load_progress(database, plan_id, len(items))
        after = await capacity_report(database, session.path)
        return MaintenanceExecutionReport(plan=plan, progress=progress, after=after)


class SQLiteStoreMaintenance:
    """Session共库维护Owner；计划不可变，进度与数据变更同事务推进。"""

    def __init__(
        self,
        session: SQLiteSessionStore,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._fault = fault or (lambda _: None)

    def _require_owner(self) -> object:
        owner = self._session._runtime_owner_token
        if owner is None:
            raise KernelError("maintenance_runtime_required", "Store维护需要活跃Session宿主")
        return owner

    async def snapshot(self) -> StoreCapacityReport:
        with storage_errors():
            async with self._session._connection() as database:
                await database.execute("BEGIN")
                return await capacity_report(database, self._session.path)

    async def plan(self, policy: RetentionPolicy) -> MaintenancePlan:
        owner = self._require_owner()
        with storage_errors():
            async with self._session._connection() as database:
                await database.execute("BEGIN IMMEDIATE")
                before = await capacity_report(database, self._session.path)
                items, protected = build_candidates(
                    policy,
                    facts=await load_thread_facts(database),
                    artifacts=await artifact_rows(database),
                    requests=await request_rows(database),
                )
                plan = _new_plan(policy, before, items, protected)
                await save_plan(database, plan, items)
                if self._session._runtime_owner_token is not owner:
                    raise KernelError("maintenance_runtime_required", "Store维护规划期间宿主已关闭")
                await database.commit()
            self._fault("maintenance.after_plan_commit")
            return plan

    async def load_plan(self, plan_id: UUID) -> MaintenancePlan:
        with storage_errors():
            plan, _, _ = await _current_plan(self._session, plan_id)
            return plan

    async def progress(self, plan_id: UUID) -> MaintenanceProgress:
        with storage_errors():
            _, _, progress = await _current_plan(self._session, plan_id)
            return progress

    async def execute(
        self,
        plan_id: UUID,
        *,
        backup_path: str | Path,
        batch_size: int = 100,
    ) -> MaintenanceExecutionReport:
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise KernelError("maintenance_invalid", "维护事务批次大小无效")
        owner = self._require_owner()
        backup = await asyncio.to_thread(resolved_path, backup_path)
        if backup == self._session.path:
            raise KernelError("maintenance_invalid", "维护备份不能覆盖Session数据库")
        with storage_errors():
            plan, items, progress = await _current_plan(self._session, plan_id)
            if progress.state == "planned":
                await _start_execution(self._session, plan, items, backup, owner, self._fault)
            else:
                await _require_same_backup(backup, progress)
            await run_batches(
                self._session,
                plan_id,
                batch_size=batch_size,
                owner=owner,
                fault=self._fault,
            )
            return await _execution_result(self._session, plan_id)

    async def restore(self, backup_path: str | Path) -> StoreRestoreReport:
        owner = self._require_owner()
        backup = await asyncio.to_thread(resolved_path, backup_path)
        if backup == self._session.path:
            raise KernelError("maintenance_invalid", "恢复源不能是当前Session数据库")
        digest_value = await asyncio.to_thread(restore_database, self._session.path, backup)
        if self._session._runtime_owner_token is not owner:
            raise KernelError("maintenance_runtime_required", "Store恢复期间宿主已关闭")
        await self._session.initialize()
        return StoreRestoreReport(
            restored_at=datetime.now(UTC),
            backup_sha256=digest_value,
            capacity=await self.snapshot(),
        )


def _new_plan(
    policy: RetentionPolicy,
    before: StoreCapacityReport,
    items: list[PlanItem],
    protected: Counter[str],
) -> MaintenancePlan:
    counts: Counter[MaintenanceItemKind] = Counter(item.kind for item in items)
    return MaintenancePlan(
        plan_id=new_id(),
        policy=policy,
        schema_version=before.schema_version,
        created_at=datetime.now(UTC),
        before=before,
        candidates_by_kind=dict(counts),
        protected_by_reason=dict(sorted(protected.items())),
        candidate_set_sha256=candidate_set_sha256(items),
    )


async def _require_same_backup(backup: Path, progress: MaintenanceProgress) -> None:
    backup_sha256 = await asyncio.to_thread(verify_backup, backup)
    if backup_sha256 != progress.backup_sha256:
        raise KernelError("maintenance_backup_changed", "维护恢复使用了不同备份")
