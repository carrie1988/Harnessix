"""产品Git交付前置：只导出本会话成功Patch连续链，并核对完整当前最终版本。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_IMAGE_BYTES,
    WorkspaceFileVersion,
    WorkspaceMutation,
)
from harnessix.delivery.planner import _read_existing
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.workspace_patch_source import (
    OwnedWorkspacePatch,
    completed_workspace_patches,
    load_owned_workspace_patch,
)
from harnessix.product_config.workspace_patch_source_contracts import (
    ProductGitDeliverySource,
    product_git_delivery_source_digest,
)
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.contracts import WorkspaceResourceRequest, WorkspaceSnapshot
from harnessix.workspace.paths import path_comparison_key
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot


def _owned_selection(
    thread: Thread,
    targets: tuple[UUID, ...],
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    checkpoint: Callable[[], None],
) -> tuple[OwnedWorkspacePatch, ...]:
    """全集合完成会话预检后才读原账本，选择顺序由持久Result而非模型参数决定。"""
    if (
        type(targets) is not tuple
        or not 1 <= len(targets) <= 256
        or any(type(target) is not UUID for target in targets)
        or len(set(targets)) != len(targets)
    ):
        raise KernelError("git_delivery_source_selection_invalid", "Git交付事务选择无效")
    selected = [
        item for item in completed_workspace_patches(thread) if item.transaction_id in targets
    ]
    if len(selected) != len(targets) or {item.transaction_id for item in selected} != set(targets):
        raise KernelError("git_delivery_source_not_owned", "Git交付来源不属于本会话成功修改")
    owned = []
    for item in selected:
        checkpoint()
        try:
            owned.append(
                load_owned_workspace_patch(thread, item.transaction_id, router, transactions)
            )
        except KernelError as error:
            codes = {
                "workspace_patch_source_not_owned": "git_delivery_source_not_owned",
                "workspace_patch_source_not_published": "git_delivery_source_not_published",
            }
            if error.code not in codes:
                raise
            raise KernelError(codes[error.code], "Git交付原Patch来源不成立") from None
    return tuple(owned)


def _merge_versions(
    owned: tuple[OwnedWorkspacePatch, ...], checkpoint: Callable[[], None]
) -> dict[str, tuple[WorkspaceFileVersion, WorkspaceFileVersion]]:
    """保留首次before与最终after；中间缺口、根身份变化和镜像容量超限明确拒绝。"""
    base = owned[0].record.plan.source
    versions: dict[str, tuple[WorkspaceFileVersion, WorkspaceFileVersion]] = {}
    for patch in owned:
        checkpoint()
        source = patch.record.plan.source
        if (
            source.platform,
            source.workspace_id,
            source.root_path_digest,
            source.root_identity,
        ) != (base.platform, base.workspace_id, base.root_path_digest, base.root_identity):
            raise KernelError("git_delivery_source_changed", "Git交付来源Workspace身份不一致")
        for mutation in patch.record.plan.mutations:
            previous = versions.get(mutation.path)
            if previous is not None and previous[1] != mutation.before:
                raise KernelError("git_delivery_source_chain_broken", "Git交付修改链不连续")
            versions[mutation.path] = (
                mutation.before if previous is None else previous[0],
                mutation.after,
            )
    if (
        len(versions) > 255
        or sum(before.size + after.size for before, after in versions.values())
        > MAX_TRANSACTION_IMAGE_BYTES
    ):
        raise KernelError("git_delivery_source_limit", "Git交付来源超过原Snapshot或镜像容量")
    return versions


def _observe_final_versions(
    root: Path,
    base: WorkspaceSnapshot,
    versions: dict[str, tuple[WorkspaceFileVersion, WorkspaceFileVersion]],
    checkpoint: Callable[[], None],
) -> WorkspaceSnapshot:
    """复用原生安全端口观察，净零路径也必须核对，读取后再验证当前Snapshot。"""
    current_root = capture_workspace_snapshot(root, platform=base.platform)
    if (current_root.workspace_id, current_root.root_path_digest, current_root.root_identity) != (
        base.workspace_id,
        base.root_path_digest,
        base.root_identity,
    ):
        raise KernelError("git_delivery_source_changed", "Git交付当前Workspace根已变化")
    checkpoint()
    snapshot = capture_workspace_snapshot(
        root,
        platform=base.platform,
        resources=tuple(WorkspaceResourceRequest(path=path, access="read") for path in versions),
    )
    if (snapshot.workspace_id, snapshot.root_path_digest, snapshot.root_identity) != (
        base.workspace_id,
        base.root_path_digest,
        base.root_identity,
    ):
        raise KernelError("git_delivery_source_changed", "Git交付当前Workspace根已变化")
    observed = {item.path: item for item in snapshot.resources}
    for path, (_, after) in versions.items():
        checkpoint()
        observation = observed[path]
        if observation.kind == "missing":
            actual = WorkspaceFileVersion(presence="absent", size=0)
        elif observation.kind == "file":
            body, mode = _read_existing(root, path, base.platform)
            actual = WorkspaceFileVersion(
                presence="file", sha256=hashlib.sha256(body).hexdigest(), size=len(body), mode=mode
            )
            if actual.sha256 != observation.content_sha256 or actual.size != observation.size:
                raise KernelError("git_delivery_source_changed", "Git交付来源读取期间变化")
        else:
            raise KernelError("git_delivery_source_changed", "Git交付目标不再是普通文件")
        if actual != after:
            raise KernelError("git_delivery_source_changed", "Git交付目标已包含后续修改")
        checkpoint()
    verify_workspace_snapshot(snapshot, root)
    return snapshot


def collect_git_delivery_source(
    thread: Thread,
    targets: tuple[UUID, ...],
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    *,
    checkpoint: Callable[[], None],
) -> ProductGitDeliverySource:
    """从宿主已认证Thread派生只读来源；输出Digest不是批准或可转交的身份凭据。"""
    checkpoint()
    owned = _owned_selection(thread, targets, router, transactions, checkpoint)
    versions = _merge_versions(owned, checkpoint)
    checkpoint()
    workspace = _observe_final_versions(
        Path(thread.workspace), owned[0].record.plan.source, versions, checkpoint
    )
    mutations = tuple(
        WorkspaceMutation(path=path, before=before, after=after)
        for path, (before, after) in sorted(
            versions.items(), key=lambda pair: path_comparison_key(pair[0], workspace.platform)
        )
        if before != after
    )
    if not mutations:
        raise KernelError("git_delivery_source_no_change", "Git交付来源没有净变化")
    candidate = ProductGitDeliverySource.model_construct(
        thread_id=thread.thread_id,
        patches=tuple(item.reference for item in owned),
        workspace=workspace,
        mutations=mutations,
        digest="0" * 64,
    )
    result = ProductGitDeliverySource(
        **candidate.model_dump(exclude={"digest"}),
        digest=product_git_delivery_source_digest(candidate),
    )
    checkpoint()
    return result
