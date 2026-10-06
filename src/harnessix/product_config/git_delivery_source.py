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
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionRecordV2
from harnessix.product_config.git_parent_contracts import ProductGitDeliverySourceV2
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
from harnessix.workspace.contracts import PlatformKind, WorkspaceResourceRequest, WorkspaceSnapshot
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.paths import path_comparison_key
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot
from harnessix.workspace.snapshot_capture import capture_snapshot_facts
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.snapshot_v2 import (
    capture_workspace_snapshot_v2,
    verify_workspace_snapshot_v2,
)


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

    def reader_checkpoint() -> None:
        try:
            checkpoint()
        except BaseException as error:
            # 控制异常不能进入下方归属错误码映射，即使二者错误码相同。
            raise UpstreamCheckpointError(error) from None

    owned = []
    for item in selected:
        checkpoint()
        try:
            owned.append(
                load_owned_workspace_patch(
                    thread, item.transaction_id, router, transactions, checkpoint=reader_checkpoint
                )
            )
        except UpstreamCheckpointError as error:
            raise error.error from None
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
    base: WorkspaceSnapshot | WorkspaceSnapshotV2,
    versions: dict[str, tuple[WorkspaceFileVersion, WorkspaceFileVersion]],
    checkpoint: Callable[[], None],
    snapshot_ports: WorkspaceSnapshotPorts | None = None,
) -> WorkspaceSnapshot | WorkspaceSnapshotV2:
    """复用原生安全端口观察，净零路径也必须核对，读取后再验证当前Snapshot。"""
    facts = capture_snapshot_facts(
        root,
        cwd=".",
        resources=(),
        external_roots=None,
        platform=base.platform,
        checkpoint=checkpoint,
    )
    if any(
        facts.scope[name] != getattr(base, name)
        for name in ("workspace_id", "root_path_digest", "root_identity")
    ):
        raise KernelError("git_delivery_source_changed", "Git交付当前Workspace根已变化")
    checkpoint()
    resources = tuple(WorkspaceResourceRequest(path=path, access="read") for path in versions)
    snapshot: WorkspaceSnapshot | WorkspaceSnapshotV2
    if snapshot_ports is None:
        snapshot = capture_workspace_snapshot(root, platform=base.platform, resources=resources)
    else:
        snapshot = capture_workspace_snapshot_v2(
            root,
            platform=base.platform,
            resources=resources,
            checkpoint=checkpoint,
            write_blob=snapshot_ports.write_blob,
            read_blob=snapshot_ports.read_blob,
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
            if isinstance(snapshot, WorkspaceSnapshotV2):
                body, mode = _read_existing(root, path, base.platform, checkpoint=checkpoint)
            else:
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
    _verify_final_snapshot(snapshot, root, checkpoint, snapshot_ports)
    return snapshot


def _verify_final_snapshot(
    snapshot: WorkspaceSnapshot | WorkspaceSnapshotV2,
    root: Path,
    checkpoint: Callable[[], None],
    ports: WorkspaceSnapshotPorts | None,
) -> None:
    """实际代际复核共享调用方控制，不新增读取期限或重捕获历史。"""
    if isinstance(snapshot, WorkspaceSnapshotV2):
        if ports is None:
            raise KernelError("workspace_closure_unavailable", "完整Workspace历史端口不可用")
        verify_workspace_snapshot_v2(
            snapshot, root, checkpoint=checkpoint, read_blob=ports.read_blob
        )
    else:
        verify_workspace_snapshot(snapshot, root)


def collect_git_delivery_source(
    thread: Thread,
    targets: tuple[UUID, ...],
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    *,
    checkpoint: Callable[[], None],
    snapshot_ports: WorkspaceSnapshotPorts | None = None,
) -> ProductGitDeliverySource:
    """从已认证Thread派生来源；显式CAS追加与输出Digest均不授予执行批准。"""
    checkpoint()
    owned = _owned_selection(thread, targets, router, transactions, checkpoint)
    versions = _merge_versions(owned, checkpoint)
    use_parent_history = any(
        isinstance(item.record, WorkspaceTransactionRecordV2) for item in owned
    )
    if use_parent_history and snapshot_ports is None:
        raise KernelError("workspace_closure_unavailable", "完整Workspace历史端口不可用")
    checkpoint()
    workspace = _observe_final_versions(
        Path(thread.workspace),
        owned[0].record.plan.source,
        versions,
        checkpoint,
        snapshot_ports if use_parent_history else None,
    )
    mutations = _net_mutations(versions, workspace.platform, checkpoint)
    model = ProductGitDeliverySourceV2 if use_parent_history else ProductGitDeliverySource
    candidate = model.model_construct(
        thread_id=thread.thread_id,
        patches=tuple(item.reference for item in owned),
        workspace=workspace,
        mutations=mutations,
        digest="0" * 64,
    )
    result = model(
        **candidate.model_dump(exclude={"digest"}),
        digest=product_git_delivery_source_digest(candidate),
    )
    checkpoint()
    return result


def _net_mutations(
    versions: dict[str, tuple[WorkspaceFileVersion, WorkspaceFileVersion]],
    platform: PlatformKind,
    checkpoint: Callable[[], None],
) -> tuple[WorkspaceMutation, ...]:
    """采集与只读复核共用原净变更组成，净零路径仍由完整versions单独验真。"""
    mutations = []
    for path, (before, after) in sorted(
        versions.items(), key=lambda pair: path_comparison_key(pair[0], platform)
    ):
        checkpoint()
        if before != after:
            mutations.append(WorkspaceMutation(path=path, before=before, after=after))
    if not mutations:
        raise KernelError("git_delivery_source_no_change", "Git交付来源没有净变化")
    return tuple(mutations)


def verify_git_delivery_source(
    thread: Thread,
    source: ProductGitDeliverySourceV2,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    *,
    checkpoint: Callable[[], None],
    snapshot_ports: WorkspaceSnapshotPorts,
) -> None:
    """已认证Thread下只读复核原Patch和所有最终版本；不重捕获、不追加CAS。"""
    checkpoint()
    if type(source) is not ProductGitDeliverySourceV2 or source.thread_id != thread.thread_id:
        raise KernelError("git_delivery_source_not_owned", "Git交付来源不属于原认证会话")
    owned = _owned_selection(
        thread,
        tuple(item.transaction_id for item in source.patches),
        router,
        transactions,
        checkpoint,
    )
    versions = _merge_versions(owned, checkpoint)
    base, workspace = owned[0].record.plan.source, source.workspace
    if (
        tuple(item.reference for item in owned) != source.patches
        or _net_mutations(versions, workspace.platform, checkpoint) != source.mutations
        or any(
            getattr(base, name) != getattr(workspace, name)
            for name in ("platform", "workspace_id", "root_path_digest", "root_identity")
        )
        or workspace.cwd != "."
        or workspace.external_roots
        or {(item.location, item.path, item.access) for item in workspace.resources}
        != {("workspace", path, "read") for path in (*versions, ".")}
    ):
        raise KernelError("git_delivery_source_changed", "Git交付完整来源与原Patch链不一致")
    root = Path(thread.workspace)
    _verify_final_snapshot(workspace, root, checkpoint, snapshot_ports)
    observed = {item.path: item for item in workspace.resources}
    for path, (_, expected) in versions.items():
        checkpoint()
        if observed[path].kind == "missing":
            actual = WorkspaceFileVersion(presence="absent", size=0)
        elif observed[path].kind == "file":
            body, mode = _read_existing(root, path, workspace.platform, checkpoint=checkpoint)
            actual = WorkspaceFileVersion(
                presence="file", sha256=hashlib.sha256(body).hexdigest(), size=len(body), mode=mode
            )
        else:
            raise KernelError("git_delivery_source_changed", "Git交付目标不再是普通文件")
        if actual != expected:
            raise KernelError("git_delivery_source_changed", "Git交付最终版本或模式已经变化")
    _verify_final_snapshot(workspace, root, checkpoint, snapshot_ports)
    checkpoint()
