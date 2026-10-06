"""原 CAS 全目录内容的只读重验；不认证归属、账本、批准、耐久或业务效果。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import cast

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_inventory_contracts import (
    GitInventoryObject,
    GitInventoryRoots,
    GitInventoryScope,
    GitObjectInventory,
    snapshot_git_inventory_scope,
    snapshot_git_object_inventory,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_object_references import (
    GitCommitReferences,
    GitTreeEntry,
    parse_git_commit,
    parse_git_tree,
)
from harnessix.delivery.git_tree_closure import GitTreeClosure, verify_git_tree_closure


def _mismatch() -> KernelError:
    """生成不含正文、路径、OID或底层异常文本的固定内容差异错误。"""
    return KernelError("git_inventory_materials_mismatch", "Git对象目录实际材料与完整声明不一致")


def _read_object(
    cas: GitMaterialCAS,
    node: GitInventoryObject,
    inventory: GitObjectInventory | GitInventoryScope,
    *,
    checkpoint: Callable[[], None],
) -> GitInventoryObject:
    """完整重读一个对象，从实际字节重算类型OID并比较全部直接引用。"""
    checkpoint()
    read = cas.read(node.material)
    checkpoint()
    if type(read) is not GitObjectMaterial:
        raise _mismatch()
    material = GitObjectMaterial.from_body(read.object_type, read.object_format, read.body)
    checksum = material.body_sha256
    reference = GitObjectMaterialReference(
        material.object_type,
        material.object_id,
        material.object_format,
        checksum,
        material.body_bytes,
        checksum,
    )
    checkpoint()
    if reference != node.material:
        raise _mismatch()
    entries: tuple[GitTreeEntry, ...] = ()
    references: GitCommitReferences | None = None
    if material.object_type == "tree":
        entries = parse_git_tree(
            material, max_entries=inventory.limits.max_entries, checkpoint=checkpoint
        )
    elif material.object_type == "commit":
        references = parse_git_commit(
            material, max_parents=inventory.max_parents, checkpoint=checkpoint
        )
    checkpoint()
    if entries != node.tree_entries or references != node.commit_references:
        raise _mismatch()
    return GitInventoryObject(reference, node.roles, entries, references)


def _actual_closure(
    cas: GitMaterialCAS,
    inventory: GitObjectInventory | GitInventoryScope,
    root: GitObjectRead,
    catalog: dict[str, GitInventoryObject],
    entries: int,
    depth: int,
    *,
    checkpoint: Callable[[], None],
) -> GitTreeClosure:
    """复用原实际完整树观察，精确核对本根对象、正文量和逐路径统计。"""
    checkpoint()
    closure = verify_git_tree_closure(
        cas,
        catalog[root.object_id].material,
        tuple(node.material for node in inventory.objects),
        platform=inventory.platform,
        limits=inventory.limits,
        checkpoint=checkpoint,
    )
    checkpoint()
    if closure.root != catalog[root.object_id].material or (
        closure.expanded_entries,
        closure.tree_depth,
    ) != (entries, depth):
        raise _mismatch()
    body_bytes = 0
    for reference in closure.objects:
        checkpoint()
        node = catalog.get(reference.object_id)
        if node is None or node.material != reference:
            raise _mismatch()
        body_bytes += reference.body_bytes
    if closure.body_bytes != body_bytes:
        raise _mismatch()
    checkpoint()
    return closure


def _complete_union(
    catalog: dict[str, GitInventoryObject],
    roots: GitInventoryRoots,
    base: GitTreeClosure,
    target: GitTreeClosure,
    *,
    checkpoint: Callable[[], None],
) -> None:
    """两实际树和业务commit根必须精确覆盖完整目录，不扩张外部父历史。"""
    observed = {roots.base_commit.object_id}
    if roots.delivery_commit is not None:
        observed.add(roots.delivery_commit.object_id)
    for closure in (base, target):
        for reference in closure.objects:
            checkpoint()
            observed.add(reference.object_id)
    if observed != catalog.keys():
        raise _mismatch()
    checkpoint()


def _verify_materials[T: GitObjectInventory | GitInventoryScope](
    cas: GitMaterialCAS, declared: T, *, checkpoint: Callable[[], None]
) -> T:
    """只复用两种完整声明的原 CAS、双树和精确并集算法，不提供扩展回调。"""
    if type(cas) is not GitMaterialCAS:
        raise KernelError("git_inventory_materials_invalid", "Git对象目录材料读取端口无效")
    nodes: list[GitInventoryObject] = []
    catalog: dict[str, GitInventoryObject] = {}
    for node in declared.objects:
        checkpoint()
        observed = _read_object(cas, node, declared, checkpoint=checkpoint)
        nodes.append(observed)
        catalog[observed.material.object_id] = observed
    actual = replace(declared, objects=tuple(nodes))
    base = _actual_closure(
        cas,
        actual,
        actual.roots.base_tree,
        catalog,
        actual.metrics.base_expanded_entries,
        actual.metrics.base_tree_depth,
        checkpoint=checkpoint,
    )
    target = _actual_closure(
        cas,
        actual,
        actual.roots.target_tree,
        catalog,
        actual.metrics.target_expanded_entries,
        actual.metrics.target_tree_depth,
        checkpoint=checkpoint,
    )
    _complete_union(catalog, actual.roots, base, target, checkpoint=checkpoint)
    # 两种快照均重新派生完整图；持久目录额外重验原 SHA，不修复输入声明。
    result: GitObjectInventory | GitInventoryScope
    if type(actual) is GitObjectInventory:
        result = snapshot_git_object_inventory(actual, checkpoint=checkpoint)
    else:
        result = snapshot_git_inventory_scope(actual, checkpoint=checkpoint)
    checkpoint()
    return cast(T, result)


def verify_git_inventory_materials(
    cas: GitMaterialCAS, inventory: object, *, checkpoint: Callable[[], None]
) -> GitObjectInventory:
    """原CAS全成员及两树重新验内容后返回新元数据；不是授权或共同原子快照。"""
    checkpoint()
    declared = snapshot_git_object_inventory(inventory, checkpoint=checkpoint)
    return _verify_materials(cas, declared, checkpoint=checkpoint)


def verify_git_inventory_scope_materials(
    cas: GitMaterialCAS, scope: object, *, checkpoint: Callable[[], None]
) -> GitInventoryScope:
    """重验无绑定声明的全部实际材料；不授予写权限或产生持久业务证明。"""
    checkpoint()
    declared = snapshot_git_inventory_scope(scope, checkpoint=checkpoint)
    return _verify_materials(cas, declared, checkpoint=checkpoint)
