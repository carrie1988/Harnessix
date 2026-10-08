"""Checkpoint规划的完整对象采集；只借原受控读取，不遍历外部提交历史。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import get_ident

from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectFormat, GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_object_references import parse_git_commit, parse_git_tree
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.git_tree_diff import GitTreeDiff
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_baseline import _root_binding_matches
from harnessix.product_config.git_checkpoint_scope import build_product_git_checkpoint_scope
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot
from harnessix.product_config.git_delivery_process import (
    GitDeliveryProcess,
    GitOperationBudget,
    PreparedGitProcess,
)
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.product_config.git_repository_observation import (
    GitRepositoryReadAuthorization,
    _authorize_read,
)


def _invalid() -> KernelError:
    """固定错误不公开用户路径、对象或配置正文。"""
    return KernelError("git_checkpoint_materials_invalid", "Git完整规划材料不符合原读取约束")


async def _read_material(
    port: GitDeliveryProcess,
    root: Path,
    request: GitObjectRead,
    authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]],
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
) -> GitObjectMaterial:
    """固定cat-file batch、原正式Plan和Owner全流认证共同证明完整材料。"""
    checkpoint()
    prepared = port.prepare_object_read(root, request, budget=budget)
    approval = await _authorize_read(authorize, prepared, cancel, budget)
    checkpoint()
    result = await port.run(
        prepared, approval.plan, cancel, budget=budget, checkpoint=approval.approval
    )
    checkpoint()
    material = result.material
    if type(material) is not GitObjectMaterial or (
        material.object_type,
        material.object_id,
        material.object_format,
    ) != (request.object_type, request.object_id, request.object_format):
        raise _invalid()
    return material


async def collect_product_git_checkpoint_materials(
    port: GitDeliveryProcess,
    root: Path,
    baseline: ProductGitDeliveryBaselineV2,
    cas: GitMaterialCAS,
    *,
    authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]],
    limits: GitTreeClosureLimits,
    max_parents: int,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
) -> tuple[GitInventoryScope, GitTreeDiff]:
    """完整base树与全部净变更进入原CAS；无递归rev-list、截断或用户Index写入。"""
    await asyncio.sleep(0)
    if (
        type(port) is not GitDeliveryProcess
        or type(port._runtime_host) is not GitProcessRuntimeHost
        or type(baseline) is not ProductGitDeliveryBaselineV2
        or type(cas) is not GitMaterialCAS
        or type(cancel) is not CancelToken
        or type(budget) is not GitOperationBudget
        or type(limits) is not GitTreeClosureLimits
        or type(cas.store) is not SQLiteWorkspaceTransactionStore
    ):
        raise _invalid()
    check = _material_control(port, root, lambda: baseline, cas, cancel, budget, checkpoint)
    check()
    baseline = _snapshot(baseline, ProductGitDeliveryBaselineV2, check)
    fmt: GitObjectFormat = "sha1" if len(baseline.head_oid) == 40 else "sha256"
    references = await _collect_base(
        port, root, baseline, cas, authorize, limits, max_parents, cancel, budget, check, fmt
    )
    after = _collect_after(cas, baseline, fmt, check)
    result = build_product_git_checkpoint_scope(
        cas,
        baseline,
        references[baseline.head_oid],
        tuple(references.values()),
        tuple(after.values()),
        limits=limits,
        max_parents=max_parents,
        checkpoint=check,
    )
    check()
    return result


def _material_control(
    port: GitDeliveryProcess,
    root: Path,
    current_baseline: Callable[[], ProductGitDeliveryBaselineV2],
    cas: GitMaterialCAS,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
) -> Callable[[], None]:
    """同Task借原局部闭包；完整根检查仍跟随调用方已深快照的原基线。"""
    host, runner, state, protection = (
        port._runtime_host,
        port._runner,
        port._state,
        port._output_redaction,
    )
    if type(host) is not GitProcessRuntimeHost:
        raise _invalid()
    store = cas.store

    def control() -> None:
        cancel.checkpoint()
        budget.remaining()
        checkpoint()
        if (
            port._closed
            or port._runtime_host is not host
            or port._runner is not runner
            or port._state != state
            or port._output_redaction is not protection
            or protection is not host.protection
            or cas.store is not store
            or store._closed
            or store._root != state / "workspace-transactions"
        ):
            raise _invalid()
        host.checkpoint(state)
        if not _root_binding_matches(current_baseline().source, root, checkpoint):
            raise _invalid()

    full = parent_cancel_checkpointer(control)
    if type(checkpoint) is not GitAuthenticationControl:
        return full
    origin = GitAuthenticationControl._binding(checkpoint)
    if (
        origin[2] is not asyncio.current_task()
        or type(origin[3]) is not int
        or origin[3] != get_ident()
    ):
        return full
    parent_fields = tuple(
        zip(
            ("_task", "_thread", "_local_check", "_authenticate"),
            (origin[2], origin[3], origin[0], origin[1]),
            strict=True,
        )
    )
    original_local = origin[0]
    store_root = store._root

    def local() -> None:
        cancel.checkpoint()
        budget.remaining()
        if type(checkpoint) is not GitAuthenticationControl:
            raise _invalid()
        current = vars(checkpoint)
        if type(current) is not dict:
            raise _invalid()
        fields = tuple(current.items())
        if any(type(name) is not str for name, _ in fields):
            raise _invalid()
        current = dict(fields)
        if any(current.get(name) is not value for name, value in parent_fields):
            raise _invalid()
        GitAuthenticationControl._binding(checkpoint, origin)
        original_local()
        if (
            type(port) is not GitDeliveryProcess
            or type(cas) is not GitMaterialCAS
            or type(store) is not SQLiteWorkspaceTransactionStore
            or type(host) is not GitProcessRuntimeHost
            or getattr(port, "_closed", None) is not False
            or getattr(port, "_runtime_host", None) is not host
            or getattr(port, "_runner", None) is not runner
            or getattr(port, "_state", None) is not state
            or getattr(port, "_output_redaction", None) is not protection
            or getattr(host, "protection", None) is not protection
            or getattr(cas, "store", None) is not store
            or getattr(store, "_closed", None) is not False
            or getattr(store, "_root", None) is not store_root
        ):
            raise _invalid()

    return GitAuthenticationControl(parent_cancel_checkpointer(local), full)


async def _collect_base(
    port: GitDeliveryProcess,
    root: Path,
    baseline: ProductGitDeliveryBaselineV2,
    cas: GitMaterialCAS,
    authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]],
    limits: GitTreeClosureLimits,
    max_parents: int,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    fmt: GitObjectFormat,
) -> dict[str, GitObjectMaterialReference]:
    """有限完整对象遍历；读取基线提交的一阶父边而不遍历提交历史。"""
    pending = [(GitObjectRead("commit", baseline.head_oid, fmt), 0)]
    references: dict[str, GitObjectMaterialReference] = {}
    total_bytes = 0
    while pending:
        check()
        request, depth = pending.pop()
        known = references.get(request.object_id)
        if known is not None:
            if (known.object_type, known.object_format) != (request.object_type, fmt):
                raise _invalid()
            continue
        if len(references) >= limits.max_objects or depth > limits.max_depth:
            raise KernelError("git_inventory_limit", "Git完整材料超过原范围预算")
        material = await _read_material(port, root, request, authorize, cancel, budget, check)
        total_bytes += material.body_bytes
        if total_bytes > limits.max_body_bytes:
            raise KernelError("git_inventory_limit", "Git完整材料超过原范围预算")
        check()
        references[request.object_id] = cas.persist(material)
        check()
        if request.object_type == "commit":
            commit = parse_git_commit(material, max_parents=max_parents, checkpoint=check)
            if commit.tree.object_id != baseline.head_tree_oid:
                raise _invalid()
            # 原parent顺序由scope保留；不把外部历史对象加入本业务材料目录。
            pending.append((commit.tree, 0))
        elif request.object_type == "tree":
            for entry in reversed(
                parse_git_tree(material, max_entries=limits.max_entries, checkpoint=check)
            ):
                check()
                if entry.mode not in {"40000", "100644", "100755"}:
                    raise _invalid()
                pending.append((entry.child, depth + (entry.child.object_type == "tree")))
    return references


def _collect_after(
    cas: GitMaterialCAS,
    baseline: ProductGitDeliveryBaselineV2,
    fmt: GitObjectFormat,
    check: Callable[[], None],
) -> dict[str, GitObjectMaterialReference]:
    """全部原Patch正文严格核对版本后转换为原blob材料；不忽略缺失文件。"""
    after: dict[str, GitObjectMaterialReference] = {}
    for mutation in baseline.source.mutations:
        check()
        if mutation.after.presence != "file":
            continue
        if mutation.after.sha256 is None:
            raise _invalid()
        body = cas.store.blob(mutation.after.sha256, checkpoint=check)
        material = GitObjectMaterial.from_body("blob", fmt, body)
        if (material.body_sha256, material.body_bytes) != (
            mutation.after.sha256,
            mutation.after.size,
        ):
            raise _invalid()
        after[material.object_id] = cas.persist(material)
        check()
    return after
