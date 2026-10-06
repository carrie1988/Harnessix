"""全对象范围与正式交付 Core 的同源复核；不执行 Git、不授予原批准。"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES
from harnessix.delivery.git import _commit_bytes
from harnessix.delivery.git_inventory_materials import verify_git_inventory_scope_materials
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_tree_diff import prepare_git_tree_diff
from harnessix.product_config.git_delivery_plan_contracts import ProductGitDeliveryCore
from harnessix.product_config.git_delivery_plan_snapshot import (
    invalid_git_delivery_plan,
    snapshot_product_git_delivery_core,
)
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure


def verify_product_git_delivery_core_materials(
    cas: GitMaterialCAS, value: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryCore:
    """全 CAS → 两树闭包 → 原净变更投影 → 完整 Diff → 原提交编码依次核验。"""
    core = snapshot_product_git_delivery_core(value, checkpoint=checkpoint)
    scope = verify_git_inventory_scope_materials(cas, core.object_scope, checkpoint=checkpoint)
    # Source2 的完整 Manifest/Chunk 仍由原唯一 CAS 读取，摘要字段不是父历史正文。
    read_workspace_parent_closure(
        core.baseline.source.workspace, cas.store.blob, checkpoint=checkpoint
    )
    catalog = tuple(node.material for node in scope.objects)
    roots = {node.material.object_id: node.material for node in scope.objects}
    after_digests = {
        mutation.after.sha256
        for mutation in core.baseline.source.mutations
        if mutation.after.presence == "file"
    }
    after_catalog = tuple(
        material
        for material in catalog
        if material.object_type == "blob" and material.body_sha256 in after_digests
    )
    diff = prepare_git_tree_diff(
        cas,
        roots[scope.roots.base_tree.object_id],
        catalog,
        core.baseline.source.mutations,
        after_catalog,
        platform=scope.platform,
        limits=scope.limits,
        checkpoint=checkpoint,
        max_diff_bytes=MAX_WORKSPACE_DIFF_BYTES,
    )
    base_files = {file.path: file for file in diff.projection.base.files}
    for member in core.baseline.members:
        checkpoint()
        actual = base_files.get(member.path)
        if (member.oid, member.mode) != (
            (None, None) if actual is None else (actual.material.object_id, actual.mode)
        ):
            raise invalid_git_delivery_plan()
    if diff.projection.root.object_id != scope.roots.target_tree.object_id or (
        diff.content.sha256,
        diff.content.utf8_bytes,
    ) != (core.diff_sha256, core.diff_bytes):
        raise invalid_git_delivery_plan()
    if core.commit_spec is not None:
        spec = core.commit_spec
        checkpoint()
        expected = GitObjectMaterial.from_body(
            "commit",
            scope.roots.base_commit.object_format,
            _commit_bytes(
                spec.tree_oid,
                spec.parent_oid,
                spec.author_name,
                spec.author_email,
                spec.authored_at,
                spec.message,
            ),
        )
        if (expected.object_id, expected.body_sha256) != (
            spec.expected_commit_oid,
            spec.raw_commit_sha256,
        ):
            raise invalid_git_delivery_plan()
        # 全范围 CAS 已证明声明对象；这里仍读取完整正文，避免只比较提交头和 OID 宽度。
        if cas.read(roots[spec.expected_commit_oid]).body != expected.body:
            raise invalid_git_delivery_plan()
    checkpoint()
    return core
