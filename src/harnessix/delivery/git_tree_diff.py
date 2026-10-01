"""完整Git目标树与Diff同源规划；不伪造Workspace事务、不持久化或授予批准。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES, WorkspaceMutation
from harnessix.delivery.diff_content import (
    DiffContentLimitError,
    WorkspaceDiffContent,
    build_diff_content,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.git_tree_projection import (
    GitTreeProjection,
    prepare_git_tree_projection,
    snapshot_git_tree_mutations,
)
from harnessix.workspace.contracts import PlatformKind


@dataclass(frozen=True, slots=True)
class GitTreeDiff:
    """本次完整内容事实；原始正文不进repr，不表示业务认证、Artifact或批准。"""

    projection: GitTreeProjection = field(repr=False)
    content: WorkspaceDiffContent = field(repr=False)
    spec_version: Literal["harnessix.git-tree-diff/v1"] = field(
        default="harnessix.git-tree-diff/v1", init=False
    )


def prepare_git_tree_diff(
    cas: GitMaterialCAS,
    root: GitObjectMaterialReference,
    catalog: tuple[GitObjectMaterialReference, ...],
    mutations: tuple[WorkspaceMutation, ...],
    after_catalog: tuple[GitObjectMaterialReference, ...],
    *,
    platform: PlatformKind,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
    max_diff_bytes: int,
) -> GitTreeDiff:
    """完整原CAS验真后重新读取Diff所需正文；任何缺失、超限或取消无部分返回。"""

    if type(max_diff_bytes) is not int or not 1 <= max_diff_bytes <= MAX_WORKSPACE_DIFF_BYTES:
        raise KernelError("git_tree_diff_limit_invalid", "Git完整Diff容量声明无效")
    # 在完整树/CAS观察前断开外层和内层别名；快照每项前仍保留取消回调。
    mutations = snapshot_git_tree_mutations(mutations, platform, checkpoint)
    projection = prepare_git_tree_projection(
        cas,
        root,
        catalog,
        mutations,
        after_catalog,
        platform=platform,
        limits=limits,
        checkpoint=checkpoint,
    )
    references: dict[str, GitObjectMaterialReference] = {}
    for file in (*projection.base.files, *projection.files):
        checkpoint()
        material = file.material
        previous = references.get(material.body_sha256)
        if previous is not None and previous != material:
            raise KernelError("git_tree_diff_invalid", "Git完整Diff正文引用不一致")
        references[material.body_sha256] = material

    def read_blob(digest: str) -> bytes:
        checkpoint()
        reference = references.get(digest)
        if reference is None:
            raise KernelError("git_tree_diff_invalid", "Git完整Diff缺少原正文引用")
        # 不相信规划期间的缓存正文：CAS二次读仍核对完整类型、SHA、长度与Git OID。
        return cas.read(reference).body

    try:
        content = build_diff_content(
            mutations,
            read_blob,
            max_utf8_bytes=max_diff_bytes,
            checkpoint=checkpoint,
            missing_newline_markers=True,
        )
    except DiffContentLimitError:
        raise KernelError("git_tree_diff_limit", "Git完整Diff超过声明容量") from None
    checkpoint()
    return GitTreeDiff(projection, content)
