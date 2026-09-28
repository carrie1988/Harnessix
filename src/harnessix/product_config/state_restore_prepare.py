"""整体恢复的有界私有副本准备；当前损坏库可保留回退，但活跃写入者仍须拒绝。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup import (
    _cleanup_unpublished_candidate,
    _quiet_databases,
    _verify,
    _verify_current_key,
    trusted_backup_manifest,
    verify_state_snapshot,
)
from harnessix.product_config.state_backup_contracts import (
    DATABASES,
    MAX_BACKUP_MANIFEST_BYTES,
    PROCESS_DATABASE,
    ProductStateBackupManifest,
)
from harnessix.product_config.state_backup_files import (
    PrivateStateTree,
    absolute_address,
    copy_file,
    create_private_tree,
    read_small,
)
from harnessix.product_config.state_backup_validation import require_backup_outside_workspaces
from harnessix.product_config.state_owner import ProductStateOwner, state_owner_anchor
from harnessix.product_config.state_restore_contracts import ProductStateRestorePlan
from harnessix.product_config.state_restore_journal import directory_identity
from harnessix.session.maintenance_io import MaintenanceIOControl


def restore_paths(owner: ProductStateOwner, restore_id: UUID) -> tuple[Path, Path]:
    """候选和原状态仅在原父目录内，固定长度UUID名称不接受外部任意路径。"""
    parent = owner.state_root.parent
    return (
        parent / f".harnessix-restore-{restore_id}.candidate",
        parent / f".harnessix-restore-{restore_id}.previous",
    )


def require_current_quiet(owner: ProductStateOwner, control: MaintenanceIOControl) -> None:
    """确认原Runtime和可读库无活跃Writer；不为损坏或缺失库注册替代Schema。"""
    if not os.path.lexists(owner.state_root):
        return
    with PrivateStateTree(owner.state_root) as tree:
        paths = tuple(
            path for path in (*DATABASES, PROCESS_DATABASE) if os.path.lexists(tree.path / path)
        )
        for path in paths:
            with tree.open_file(path):
                pass
        # 原生Windows改名前必须关闭SQLite连接；提交窗口由合作宿主的根外Owner互斥。
        with _quiet_databases(tree, paths, control, tolerate_corrupt=True):
            control.checkpoint()


def prepare_restore(
    owner: ProductStateOwner,
    backup: Path,
    restore_id: UUID,
    confirm_backup_id: UUID,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> ProductStateRestorePlan:
    manifest = _verify(owner, backup, control)
    if manifest.backup_id != confirm_backup_id:
        raise KernelError("product_restore_confirmation_mismatch", "恢复确认与原备份实例不匹配")
    require_current_quiet(owner, control)
    target = absolute_address(backup)
    candidate, previous = restore_paths(owner, restore_id)
    if os.path.lexists(candidate) or os.path.lexists(previous):
        raise KernelError("product_restore_id_conflict", "恢复请求ID的目录已有对象，禁止覆盖")
    with PrivateStateTree(target) as bundle, PrivateStateTree(target / "state") as original:
        body = read_small(bundle, "manifest.json", MAX_BACKUP_MANIFEST_BYTES)
        if trusted_backup_manifest(owner, body) != manifest:
            raise KernelError("product_backup_changed", "原备份在恢复准备期间发生变化")
        require_backup_outside_workspaces(original, owner.state_root, control)
        require_backup_outside_workspaces(original, candidate, control)
        identity = _copy_candidate(owner, original, candidate, manifest, control, fault)
    try:
        control.checkpoint()
        require_current_quiet(owner, control)
        parent = owner.state_root.parent.stat()
        if directory_identity(candidate) != identity:
            raise KernelError("product_restore_candidate_missing", "完整恢复候选身份已改变")
        return ProductStateRestorePlan(
            restore_id=restore_id,
            owner_address_key=state_owner_anchor(owner.state_root).name,
            parent_identity=(parent.st_dev, parent.st_ino),
            previous_identity=directory_identity(owner.state_root),
            candidate_identity=identity,
            manifest_json=body.decode(),
        )
    except BaseException:
        _cleanup_unpublished_candidate(owner, candidate, identity, None)
        raise


def _copy_candidate(
    owner: ProductStateOwner,
    original: PrivateStateTree,
    candidate: Path,
    manifest: ProductStateBackupManifest,
    control: MaintenanceIOControl,
    fault: Callable[[str], None],
) -> tuple[int, int]:
    create_private_tree(candidate)
    identity = directory_identity(candidate)
    if identity is None:
        raise KernelError("product_restore_candidate_missing", "完整恢复候选已丢失")
    try:
        with PrivateStateTree(candidate) as copied:
            copied.directory("workspace-transactions/blobs")
            for entry in manifest.files:
                control.checkpoint()
                copy_file(original, copied, entry.path, control)
                fault("restore.after_copy:" + entry.path)
            verify_state_snapshot(copied, manifest, control)
            _verify_current_key(owner, copied)
        return identity
    except BaseException:
        _cleanup_unpublished_candidate(owner, candidate, identity, None)
        raise
