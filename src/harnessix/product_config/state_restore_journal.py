"""原地址根外恢复Journal：不可覆盖发布、原计划Hash及粘性回退决定，不修改用户状态库。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup import trusted_backup_manifest
from harnessix.product_config.state_backup_files import (
    PrivateStateTree,
    publish_tree,
    read_small,
    write_new,
)
from harnessix.product_config.state_owner import ProductStateOwner, state_owner_anchor
from harnessix.product_config.state_restore_contracts import (
    ACTIVE_RESTORE_FILE,
    MAX_RESTORE_RECORD_BYTES,
    ProductStateRestorePlan,
    ProductStateRestorePointer,
    ProductStateRestoreResult,
    ProductStateRollbackDecision,
    journal_directory,
)


def invalid_journal() -> KernelError:
    """统一拒绝错配恢复来源，不在公开错误中携带原记录或路径。"""
    return KernelError("product_restore_journal_invalid", "恢复Journal身份、原来源或记录无效")


def directory_identity(path: Path) -> tuple[int, int] | None:
    """缺失与私有原目录严格区分；不追随叶对象链接或修复权限。"""
    if not os.path.lexists(path):
        return None
    with PrivateStateTree(path):
        info = path.lstat()
        if info.st_ino <= 0:
            raise invalid_journal()
        return info.st_dev, info.st_ino


def write_record(anchor: PrivateStateTree, name: str, body: bytes) -> None:
    """先持久化自有文件，再原生不可覆盖发布；确认异常时不删除可能已发布的记录。"""
    if not 0 < len(body) <= MAX_RESTORE_RECORD_BYTES:
        raise invalid_journal()
    temporary = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    temporary += f"record-{uuid4()}.pending"
    write_new(anchor, temporary, body)
    publish_tree(anchor.path / temporary, anchor.path / name)


def activate_restore(owner: ProductStateOwner, plan: ProductStateRestorePlan) -> None:
    """计划与活动指针必须耐久成功后，才允许原Root首次改名。"""
    owner.require_ready(owner.state_root)
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        directory = journal_directory(plan.restore_id)
        if os.path.lexists(anchor.path / directory):
            raise KernelError("product_restore_id_conflict", "恢复请求ID已有记录，禁止覆盖")
        anchor.directory(directory)
        body = plan.model_dump_json(warnings="error").encode()
        write_record(anchor, directory + "/plan.json", body)
        pointer = ProductStateRestorePointer(
            restore_id=plan.restore_id, plan_sha256=hashlib.sha256(body).hexdigest()
        )
        write_record(
            anchor, ACTIVE_RESTORE_FILE, pointer.model_dump_json(warnings="error").encode()
        )


def read_plan(
    owner: ProductStateOwner, restore_id: UUID, *, active: bool
) -> tuple[ProductStateRestorePlan, str]:
    """原指针绑定计划字节；原地址和原独立备份回执共同约束恢复授权。"""
    owner.require(owner.state_root)
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        body = read_small(
            anchor, journal_directory(restore_id) + "/plan.json", MAX_RESTORE_RECORD_BYTES
        )
        digest = hashlib.sha256(body).hexdigest()
        if active:
            pointer = ProductStateRestorePointer.model_validate_json(
                read_small(anchor, ACTIVE_RESTORE_FILE, 4096)
            )
            if pointer.restore_id != restore_id or pointer.plan_sha256 != digest:
                raise invalid_journal()
        plan = ProductStateRestorePlan.model_validate_json(body)
    parent = owner.state_root.parent.stat()
    if (
        plan.restore_id != restore_id
        or plan.owner_address_key != state_owner_anchor(owner.state_root).name
        or plan.parent_identity != (parent.st_dev, parent.st_ino)
    ):
        raise invalid_journal()
    if trusted_backup_manifest(owner, plan.manifest_json.encode()) != plan.manifest:
        raise invalid_journal()
    return plan, digest


def read_result(owner: ProductStateOwner, restore_id: UUID) -> ProductStateRestoreResult | None:
    """读取原请求的耐久结果；不存在不创建记录或推断业务成功。"""
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        name = journal_directory(restore_id) + "/result.json"
        if not os.path.lexists(anchor.path / name):
            return None
        result = ProductStateRestoreResult.model_validate_json(read_small(anchor, name, 4096))
        if result.restore_id != restore_id:
            raise invalid_journal()
        return result


def rollback_requested(owner: ProductStateOwner, restore_id: UUID, digest: str) -> bool:
    """读取与计划Hash绑定的粘性回退决定，损坏记录不能当作未选择。"""
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        name = journal_directory(restore_id) + "/rollback.json"
        if not os.path.lexists(anchor.path / name):
            return False
        decision = ProductStateRollbackDecision.model_validate_json(read_small(anchor, name, 4096))
        if decision.restore_id != restore_id or decision.plan_sha256 != digest:
            raise invalid_journal()
        return True


def request_rollback(owner: ProductStateOwner, restore_id: UUID, digest: str) -> None:
    """回退首次命名空间修改之前持久化选择，重试只能沿用原决定。"""
    if rollback_requested(owner, restore_id, digest):
        return
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        decision = ProductStateRollbackDecision(restore_id=restore_id, plan_sha256=digest)
        write_record(
            anchor,
            journal_directory(restore_id) + "/rollback.json",
            decision.model_dump_json(warnings="error").encode(),
        )


def finish_restore(owner: ProductStateOwner, result: ProductStateRestoreResult) -> None:
    """完成核对后不可覆盖发布原请求终态，再由调用方解除启动阻断。"""
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        write_record(
            anchor,
            journal_directory(result.restore_id) + "/result.json",
            result.model_dump_json(warnings="error").encode(),
        )


def clear_active_restore(owner: ProductStateOwner, restore_id: UUID, digest: str) -> None:
    """终态耐久后才撤销启动阻断；POSIX删除目录项必须同步父目录。"""
    with PrivateStateTree(state_owner_anchor(owner.state_root)) as anchor:
        pointer = ProductStateRestorePointer.model_validate_json(
            read_small(anchor, ACTIVE_RESTORE_FILE, 4096)
        )
        if pointer.restore_id != restore_id or pointer.plan_sha256 != digest:
            raise invalid_journal()
        (anchor.path / ACTIVE_RESTORE_FILE).unlink()
        if os.name == "posix":
            os.fsync(anchor._root_fd)
