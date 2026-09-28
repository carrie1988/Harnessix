"""整体状态恢复与显式结算入口；请求ID先稳定，未知提交不自动重放或创建替代Key。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup import (
    _cleanup_unpublished_candidate,
    _controlled,
    _require_budget,
)
from harnessix.product_config.state_owner import (
    ProductStateOwner,
    product_state_owner,
    state_owner_anchor,
)
from harnessix.product_config.state_restore_contracts import (
    ACTIVE_RESTORE_FILE,
    ProductStateRestoreResult,
    RestoreMode,
)
from harnessix.product_config.state_restore_flow import settle_restore
from harnessix.product_config.state_restore_journal import activate_restore, read_plan, read_result
from harnessix.product_config.state_restore_prepare import prepare_restore, restore_paths
from harnessix.session.maintenance_io import MaintenanceIOControl, run_maintenance_io


@contextmanager
def _restore_errors() -> Iterator[None]:
    try:
        yield
    except KernelError:
        raise
    except (OSError, sqlite3.Error, ValidationError, ValueError, RuntimeError):
        raise KernelError(
            "product_restore_invalid", "完整产品恢复未完成，请核对原恢复记录"
        ) from None


def _require_ids(*ids: UUID) -> None:
    if any(type(value) is not UUID for value in ids):
        raise KernelError("product_restore_confirmation_invalid", "恢复请求及确认必须使用明确UUID")


def _restore(
    owner: ProductStateOwner,
    backup: Path,
    restore_id: UUID,
    confirm_backup_id: UUID,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> ProductStateRestoreResult:
    owner.require_ready(owner.state_root)
    prior = read_result(owner, restore_id)
    if prior is not None:
        if prior.backup_id != confirm_backup_id:
            raise KernelError("product_restore_id_conflict", "恢复请求ID与原备份实例冲突")
        return prior
    plan = prepare_restore(owner, backup, restore_id, confirm_backup_id, control, fault)
    try:
        control.checkpoint()
        activate_restore(owner, plan)
    except BaseException:
        # 指针的Rename可能已经提交；仅在未激活时清理身份仍匹配的自有候选。
        if not os.path.lexists(state_owner_anchor(owner.state_root) / ACTIVE_RESTORE_FILE):
            candidate, _previous = restore_paths(owner, restore_id)
            _cleanup_unpublished_candidate(owner, candidate, plan.candidate_identity, None)
        raise
    fault("restore.after_intent")
    plan, digest = read_plan(owner, restore_id, active=True)
    return settle_restore(owner, plan, digest, "complete", control, fault)


def _recover(
    owner: ProductStateOwner,
    restore_id: UUID,
    mode: RestoreMode,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> ProductStateRestoreResult:
    active = os.path.lexists(state_owner_anchor(owner.state_root) / ACTIVE_RESTORE_FILE)
    if not active:
        prior = read_result(owner, restore_id)
        if prior is not None:
            return prior
        raise KernelError("product_restore_not_pending", "原请求没有未决恢复或耐久结果")
    plan, digest = read_plan(owner, restore_id, active=True)
    return settle_restore(owner, plan, digest, mode, control, fault)


async def restore_product_state(
    state_root: Path,
    backup: Path,
    *,
    restore_id: UUID,
    confirm_backup_id: UUID,
    budget_seconds: float = 120.0,
    fault: Callable[[str], None] | None = None,
) -> ProductStateRestoreResult:
    """显式替换完整受管Root；原目录保留，取消后先结算唯一原IO线程再释放Owner。"""
    _require_budget(budget_seconds)
    _require_ids(restore_id, confirm_backup_id)
    with _restore_errors(), product_state_owner(state_root) as owner:
        return await run_maintenance_io(
            lambda control: _controlled(
                lambda: _restore(
                    owner, backup, restore_id, confirm_backup_id, control, fault or (lambda _: None)
                ),
                control,
            ),
            budget_seconds=budget_seconds,
        )


async def recover_product_state_restore(
    state_root: Path,
    *,
    confirm_restore_id: UUID,
    mode: RestoreMode,
    budget_seconds: float = 120.0,
    fault: Callable[[str], None] | None = None,
) -> ProductStateRestoreResult:
    """沿原耐久计划明确继续或回退；已完成请求只返回原结果，不再切换任何目录。"""
    _require_budget(budget_seconds)
    _require_ids(confirm_restore_id)
    if type(mode) is not str or mode not in {"complete", "rollback"}:
        raise KernelError("product_restore_mode_invalid", "恢复结算模式必须是complete或rollback")
    with _restore_errors(), product_state_owner(state_root) as owner:
        return await run_maintenance_io(
            lambda control: _controlled(
                lambda: _recover(
                    owner, confirm_restore_id, mode, control, fault or (lambda _: None)
                ),
                control,
            ),
            budget_seconds=budget_seconds,
        )
