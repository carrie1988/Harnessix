"""Checkpoint 的完整原 CAS 材料组装；不采集 Git、不认证 Session、不授予批准。"""

from __future__ import annotations

from collections.abc import Callable
from typing import get_args

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES
from harnessix.delivery.git_authentication_control import pure_git_authentication
from harnessix.delivery.git_inventory_contracts import (
    GitBaseHistoryBoundary,
    GitInventoryMetrics,
    GitInventoryObject,
    GitInventoryRole,
    GitInventoryRoots,
    GitInventoryScope,
    _snapshot_model,
    snapshot_git_inventory_scope,
)
from harnessix.delivery.git_inventory_materials import verify_git_inventory_scope_materials
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.delivery.git_object_references import (
    GitCommitReferences,
    GitTreeEntry,
    parse_git_commit,
    parse_git_tree,
)
from harnessix.delivery.git_tree_closure import (
    GitTreeClosure,
    GitTreeClosureLimits,
    verify_git_tree_closure,
)
from harnessix.delivery.git_tree_diff import GitTreeDiff, prepare_git_tree_diff
from harnessix.product_config.git_delivery_plan_snapshot import (
    _snapshot,
    invalid_git_delivery_plan,
)
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.workspace.contracts import PlatformKind
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _catalog(
    values: tuple[GitObjectMaterialReference, ...], checkpoint: Callable[[], None]
) -> tuple[GitObjectMaterialReference, ...]:
    """复用原有限模型深快照，不接受列表、子类或被改写的引用字段。"""
    checkpoint()
    if type(values) is not tuple:
        raise invalid_git_delivery_plan()
    return tuple(_snapshot_model(value, GitObjectMaterialReference, checkpoint) for value in values)


def _request(reference: GitObjectMaterialReference) -> GitObjectRead:
    return GitObjectRead(reference.object_type, reference.object_id, reference.object_format)


def _union(
    references: tuple[GitObjectMaterialReference, ...],
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> dict[str, GitObjectMaterialReference]:
    """按 Git OID 精确去重；完整并集预算包含基线提交，不只计算变化树。"""
    result: dict[str, GitObjectMaterialReference] = {}
    body_bytes = 0
    for reference in references:
        checkpoint()
        previous = result.get(reference.object_id)
        if previous is not None:
            if previous != reference:
                raise invalid_git_delivery_plan()
            continue
        result[reference.object_id] = reference
        body_bytes += reference.body_bytes
        if len(result) > limits.max_objects or body_bytes > limits.max_body_bytes:
            raise KernelError("git_inventory_limit", "Git对象目录超过声明容量")
    checkpoint()
    return result


def _scope(
    cas: GitMaterialCAS,
    references: dict[str, GitObjectMaterialReference],
    roots: GitInventoryRoots,
    base: GitTreeClosure,
    target: GitTreeClosure,
    limits: GitTreeClosureLimits,
    max_parents: int,
    platform: PlatformKind,
    checkpoint: Callable[[], None],
) -> GitInventoryScope:
    """从实际 CAS 正文解析全部直接边，再构造原规范角色和完整计数。"""
    parsed: dict[str, tuple[tuple[GitTreeEntry, ...], GitCommitReferences | None]] = {}
    members: set[str] = set()
    for oid in sorted(references):
        checkpoint()
        material = cas.read(references[oid])
        checkpoint()
        entries: tuple[GitTreeEntry, ...] = ()
        commit: GitCommitReferences | None = None
        if material.object_type == "tree":
            entries = parse_git_tree(
                material, max_entries=limits.max_entries, checkpoint=checkpoint
            )
            for entry in entries:
                checkpoint()
                members.add(entry.child.object_id)
        elif material.object_type == "commit":
            commit = parse_git_commit(material, max_parents=max_parents, checkpoint=checkpoint)
        parsed[oid] = (entries, commit)
    nodes = []
    for oid in sorted(references):
        checkpoint()
        roles: set[GitInventoryRole] = set()
        root_roles: tuple[tuple[GitInventoryRole, GitObjectRead], ...] = (
            ("base_commit", roots.base_commit),
            ("base_tree", roots.base_tree),
            ("target_tree", roots.target_tree),
        )
        for role, request in root_roles:
            if request.object_id == oid:
                roles.add(role)
        if oid in members:
            roles.add("tree_member")
        entries, commit = parsed[oid]
        nodes.append(
            GitInventoryObject(
                references[oid],
                tuple(role for role in get_args(GitInventoryRole) if role in roles),
                entries,
                commit,
            )
        )
    base_references = parsed[roots.base_commit.object_id][1]
    if base_references is None:
        raise invalid_git_delivery_plan()
    checkpoint()
    return GitInventoryScope(
        "checkpoint",
        platform,
        roots,
        tuple(nodes),
        GitBaseHistoryBoundary(
            "base_commit_parent_edges",
            roots.base_commit,
            base_references.parents,
            tuple(sorted({parent.object_id for parent in base_references.parents})),
        ),
        limits,
        max_parents,
        GitInventoryMetrics(
            len(nodes),
            sum(node.material.body_bytes for node in nodes),
            sum(len(node.tree_entries) for node in nodes),
            len(base_references.parents),
            base.expanded_entries,
            target.expanded_entries,
            base.tree_depth,
            target.tree_depth,
        ),
    )


def _build(
    cas: GitMaterialCAS,
    baseline: ProductGitDeliveryBaselineV2,
    base_commit: GitObjectMaterialReference,
    base_catalog: tuple[GitObjectMaterialReference, ...],
    after_catalog: tuple[GitObjectMaterialReference, ...],
    limits: GitTreeClosureLimits,
    max_parents: int,
    checkpoint: Callable[[], None],
) -> tuple[GitInventoryScope, GitTreeDiff]:
    if base_commit.object_type != "commit" or base_commit.object_id != baseline.head_oid:
        raise invalid_git_delivery_plan()
    checkpoint()
    commit = cas.read(base_commit)
    checkpoint()
    parents = parse_git_commit(commit, max_parents=max_parents, checkpoint=checkpoint)
    if parents.tree.object_id != baseline.head_tree_oid:
        raise invalid_git_delivery_plan()
    root = next((ref for ref in base_catalog if ref.object_id == baseline.head_tree_oid), None)
    if root is None or _request(root) != parents.tree:
        raise invalid_git_delivery_plan()
    platform = baseline.source.workspace.platform
    diff = prepare_git_tree_diff(
        cas,
        root,
        base_catalog,
        baseline.source.mutations,
        after_catalog,
        platform=platform,
        limits=limits,
        checkpoint=checkpoint,
        max_diff_bytes=MAX_WORKSPACE_DIFF_BYTES,
    )
    base = diff.projection.base
    # 基线目录可含显式提交根，但不得夹带不属于完整基线树的历史或其他对象。
    allowed = {ref.object_id for ref in base.objects} | {base_commit.object_id}
    for reference in base_catalog:
        checkpoint()
        if reference.object_id not in allowed or (
            reference.object_id == base_commit.object_id and reference != base_commit
        ):
            raise invalid_git_delivery_plan()
    files = {file.path: file for file in base.files}
    for member in baseline.members:
        checkpoint()
        actual = files.get(member.path)
        if (member.oid, member.mode) != (
            (None, None) if actual is None else (actual.material.object_id, actual.mode)
        ):
            raise invalid_git_delivery_plan()
    new_references = []
    for material in diff.projection.new_trees:
        checkpoint()
        new_references.append(
            GitObjectMaterialReference(
                material.object_type,
                material.object_id,
                material.object_format,
                material.body_sha256,
                material.body_bytes,
                material.body_sha256,
            )
        )
    references = _union(
        (base_commit, *base.objects, *after_catalog, *new_references), limits, checkpoint
    )
    # 先核对整个图的容量，再把原投影的新树原样写入同一 CAS；不写 Git 对象库或 Ref。
    for material in diff.projection.new_trees:
        checkpoint()
        stored = cas.persist(material)
        checkpoint()
        if stored != references[material.object_id]:
            raise invalid_git_delivery_plan()
    target = verify_git_tree_closure(
        cas,
        references[diff.projection.root.object_id],
        tuple(references.values()),
        platform=platform,
        limits=limits,
        checkpoint=checkpoint,
    )
    roots = GitInventoryRoots(_request(base_commit), parents.tree, _request(target.root), None)
    scope = _scope(cas, references, roots, base, target, limits, max_parents, platform, checkpoint)
    scope = snapshot_git_inventory_scope(scope, checkpoint=checkpoint)
    scope = verify_git_inventory_scope_materials(cas, scope, checkpoint=checkpoint)
    checkpoint()
    return scope, diff


def _snapshot_inputs(
    baseline: ProductGitDeliveryBaselineV2,
    base_commit: GitObjectMaterialReference,
    base_catalog: tuple[GitObjectMaterialReference, ...],
    after_catalog: tuple[GitObjectMaterialReference, ...],
    limits: GitTreeClosureLimits,
    max_parents: int,
    checkpoint: Callable[[], None],
) -> tuple[
    ProductGitDeliveryBaselineV2,
    GitObjectMaterialReference,
    tuple[GitObjectMaterialReference, ...],
    tuple[GitObjectMaterialReference, ...],
    GitTreeClosureLimits,
    int,
]:
    """只快照声明入参；纯段边界异常不进入原控制标记的单层解包。"""
    with pure_git_authentication(checkpoint) as pure_check:

        def check() -> None:
            try:
                pure_check()
            except BaseException as error:
                raise UpstreamCheckpointError(error) from None

        try:
            baseline = _snapshot(baseline, ProductGitDeliveryBaselineV2, check)
            base_commit = _snapshot_model(base_commit, GitObjectMaterialReference, check)
            base_catalog = _catalog(base_catalog, check)
            after_catalog = _catalog(after_catalog, check)
            limits = _snapshot_model(limits, GitTreeClosureLimits, check)
            if type(max_parents) is not int or max_parents < 0:
                raise KernelError("git_inventory_limit_invalid", "Git对象目录父边容量声明无效")
            return baseline, base_commit, base_catalog, after_catalog, limits, max_parents
        except UpstreamCheckpointError as error:
            raise error.error from None


def build_product_git_checkpoint_scope(
    cas: GitMaterialCAS,
    baseline: ProductGitDeliveryBaselineV2,
    base_commit: GitObjectMaterialReference,
    base_catalog: tuple[GitObjectMaterialReference, ...],
    after_catalog: tuple[GitObjectMaterialReference, ...],
    *,
    limits: GitTreeClosureLimits,
    max_parents: int,
    checkpoint: Callable[[], None],
) -> tuple[GitInventoryScope, GitTreeDiff]:
    """深快照输入并组装完整 Checkpoint 图与净 Diff；失败不返回部分范围。

    调用方提供原 CAS、完整基线树目录、净变化后的正文引用及全部显式限额。
    本入口只持久化原投影的新树，基线父边仅声明为外部历史；不创建认证身份。
    """
    if not callable(checkpoint):
        raise invalid_git_delivery_plan()

    def check() -> None:
        # 原控制标记穿过解析器和 CAS 的异常映射；入口只解包一次，保留原异常身份。
        try:
            checkpoint()
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None

    try:
        check()
        if type(cas) is not GitMaterialCAS:
            raise KernelError("git_inventory_materials_invalid", "Git对象目录材料读取端口无效")
    except UpstreamCheckpointError as error:
        raise error.error from None

    snapshots = _snapshot_inputs(
        baseline, base_commit, base_catalog, after_catalog, limits, max_parents, checkpoint
    )
    try:
        return _build(cas, *snapshots, check)
    except UpstreamCheckpointError as error:
        raise error.error from None
