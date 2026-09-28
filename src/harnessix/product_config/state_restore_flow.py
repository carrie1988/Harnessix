"""恢复目录切换的可重入状态机；实际对象身份决定位置，错误响应不证明Rename未发生。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.state_backup import _verify_current_key, verify_state_snapshot
from harnessix.product_config.state_backup_files import PrivateStateTree, publish_tree
from harnessix.product_config.state_owner import ProductStateOwner
from harnessix.product_config.state_restore_contracts import (
    ProductStateRestorePlan,
    ProductStateRestoreResult,
    RestoreMode,
)
from harnessix.product_config.state_restore_journal import (
    clear_active_restore,
    directory_identity,
    finish_restore,
    read_result,
    request_rollback,
    rollback_requested,
)
from harnessix.product_config.state_restore_prepare import require_current_quiet, restore_paths
from harnessix.session.maintenance_io import MaintenanceIOControl


def changed_state() -> KernelError:
    """实际对象不再符合原意图时拒绝，不能通过删除或覆盖未知目录继续。"""
    return KernelError("product_restore_state_changed", "恢复目录身份或位置与原计划不一致")


def _snapshot(path: Path, plan: ProductStateRestorePlan, control: MaintenanceIOControl) -> None:
    with PrivateStateTree(path) as tree:
        verify_state_snapshot(tree, plan.manifest, control)


def _complete(
    owner: ProductStateOwner,
    plan: ProductStateRestorePlan,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> None:
    candidate, previous = restore_paths(owner, plan.restore_id)
    root_id, copy_id, old_id = (
        directory_identity(path) for path in (owner.state_root, candidate, previous)
    )
    if root_id == plan.candidate_identity:
        if copy_id is not None or old_id != plan.previous_identity:
            raise changed_state()
        _snapshot(owner.state_root, plan, control)
        return
    if copy_id != plan.candidate_identity:
        raise changed_state()
    _snapshot(candidate, plan, control)
    if plan.previous_identity is not None and root_id == plan.previous_identity and old_id is None:
        with PrivateStateTree(candidate) as tree:
            _verify_current_key(owner, tree)
        require_current_quiet(owner, control)
        control.checkpoint()
        publish_tree(owner.state_root, previous)
        fault("restore.after_previous")
        root_id, old_id = directory_identity(owner.state_root), directory_identity(previous)
    if root_id is not None or old_id != plan.previous_identity:
        raise changed_state()
    control.checkpoint()
    owner.require(owner.state_root)
    publish_tree(candidate, owner.state_root)
    fault("restore.after_publish")
    _snapshot(owner.state_root, plan, control)


def _rollback(
    owner: ProductStateOwner,
    plan: ProductStateRestorePlan,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> None:
    candidate, previous = restore_paths(owner, plan.restore_id)
    root_id, copy_id, old_id = (
        directory_identity(path) for path in (owner.state_root, candidate, previous)
    )
    if root_id == plan.previous_identity and old_id is None:
        if copy_id not in {None, plan.candidate_identity}:
            raise changed_state()
        return
    if root_id == plan.candidate_identity:
        if copy_id is not None or old_id != plan.previous_identity:
            raise changed_state()
        control.checkpoint()
        publish_tree(owner.state_root, candidate)
        fault("restore.rollback_after_candidate")
        root_id, copy_id = directory_identity(owner.state_root), directory_identity(candidate)
    if (
        root_id is not None
        or copy_id != plan.candidate_identity
        or old_id != plan.previous_identity
    ):
        raise changed_state()
    if plan.previous_identity is not None:
        control.checkpoint()
        publish_tree(previous, owner.state_root)
        fault("restore.rollback_after_previous")
    if directory_identity(owner.state_root) != plan.previous_identity:
        raise changed_state()


def settle_restore(
    owner: ProductStateOwner,
    plan: ProductStateRestorePlan,
    digest: str,
    mode: RestoreMode,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> ProductStateRestoreResult:
    """沿原计划结算命名空间；原终态先验真，回退决定先耐久，再撤销启动保护。"""
    owner.require(owner.state_root)
    prior = read_result(owner, plan.restore_id)
    if prior is not None:
        _verify_finished(owner, plan, prior, control)
        clear_active_restore(owner, plan.restore_id, digest)
        return prior
    requested = rollback_requested(owner, plan.restore_id, digest)
    if requested and mode != "rollback":
        raise KernelError("product_restore_rollback_pending", "恢复已选择回退，不能改为继续恢复")
    if mode == "rollback":
        request_rollback(owner, plan.restore_id, digest)
        _rollback(owner, plan, control, fault)
    else:
        _complete(owner, plan, control, fault)
    result = ProductStateRestoreResult(
        restore_id=plan.restore_id,
        backup_id=plan.manifest.backup_id,
        status="rolled_back" if mode == "rollback" else "restored",
        retained_previous_state=mode == "complete" and plan.previous_identity is not None,
        completed_at=utc_now(),
    )
    finish_restore(owner, result)
    fault("restore.after_result")
    clear_active_restore(owner, plan.restore_id, digest)
    return result


def _verify_finished(
    owner: ProductStateOwner,
    plan: ProductStateRestorePlan,
    result: ProductStateRestoreResult,
    control: MaintenanceIOControl,
) -> None:
    """终态已落盘不代表当前目录未丢失；确认结算只能验真，不能再次执行切换。"""
    candidate, previous = restore_paths(owner, plan.restore_id)
    root_id, copy_id, old_id = (
        directory_identity(path) for path in (owner.state_root, candidate, previous)
    )
    if result.backup_id != plan.manifest.backup_id:
        raise changed_state()
    if result.status == "restored":
        if (
            root_id != plan.candidate_identity
            or copy_id is not None
            or old_id != plan.previous_identity
            or result.retained_previous_state != (plan.previous_identity is not None)
        ):
            raise changed_state()
        _snapshot(owner.state_root, plan, control)
    elif (
        root_id != plan.previous_identity
        or old_id is not None
        or copy_id not in {None, plan.candidate_identity}
        or result.retained_previous_state
    ):
        raise changed_state()
