"""Git检查点：保护物化前内容与索引，并冻结原不可变检查点合同。"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    WorkspaceFileVersion,
    WorkspaceMutation,
    WorkspaceTransactionRecord,
)
from harnessix.delivery.git_contracts import (
    GitCheckpoint,
    ManagedGitWorktreeRecord,
    git_checkpoint_digest,
)
from harnessix.delivery.planner import _read_existing
from harnessix.execution.contracts import canonical_digest
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot


class _CheckpointGitPort(Protocol):
    """只读固定Git观察；执行器持有环境、可执行文件和超时约束。"""

    def run(self, cwd: Path, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]: ...

    def oid(self, cwd: Path, arguments: tuple[str, ...]) -> str: ...


def build_git_checkpoint(
    worktree: ManagedGitWorktreeRecord,
    transaction: WorkspaceTransactionRecord,
    tree: str,
    checkpoint_id: UUID | None,
    now: datetime | None,
) -> GitCheckpoint:
    """共用原不可变字段与Digest；不写Store、不新增Checkpoint状态。"""
    if worktree.binding is None:
        raise KernelError("git_worktree_not_ready", "Git Checkpoint要求ready worktree")
    identifier = checkpoint_id or uuid4()
    mutations_digest = canonical_digest(
        [item.model_dump(mode="json", warnings="error") for item in transaction.plan.mutations]
    )
    candidate = GitCheckpoint.model_construct(
        _fields_set=None,
        checkpoint_id=identifier,
        worktree_id=worktree.worktree_id,
        worktree_plan_fingerprint=worktree.plan.fingerprint,
        worktree_binding_digest=worktree.binding.digest,
        transaction_id=transaction.transaction_id,
        transaction_plan_fingerprint=transaction.plan.fingerprint,
        repository_binding_digest=worktree.plan.repository.digest,
        base_commit_oid=worktree.plan.repository.head_oid,
        base_tree_oid=worktree.plan.repository.head_tree_oid,
        tree_oid=tree,
        mutations_digest=mutations_digest,
        created_at=now or datetime.now(UTC),
        digest="0" * 64,
    )
    checkpoint = GitCheckpoint(
        **candidate.model_dump(exclude={"digest"}),
        digest=git_checkpoint_digest(candidate),
    )
    return checkpoint


def verify_checkpoint_worktree(
    git: _CheckpointGitPort,
    record: ManagedGitWorktreeRecord,
    mutations: tuple[WorkspaceMutation, ...],
    target_tree: str,
) -> None:
    """物化前拒绝第三内容和计划外改动；允许原before/after镜像的幂等重建。"""
    root = Path(record.plan.path)
    allowed = {item.path for item in mutations}
    changed = git.run(
        root,
        (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "--no-renames",
            "-z",
            record.plan.repository.head_oid,
            "--",
        ),
    ).stdout
    try:
        paths = {item.decode("utf-8", errors="strict") for item in changed.split(b"\0") if item}
    except UnicodeError:
        raise KernelError("git_checkpoint_diverged", "受管Worktree存在不可识别的变化") from None
    if not paths <= allowed or git.oid(root, ("write-tree",)) not in {
        record.plan.repository.head_tree_oid,
        target_tree,
    }:
        raise KernelError("git_checkpoint_diverged", "受管Worktree存在计划外文件或索引变化")
    snapshot = capture_workspace_snapshot(
        root,
        platform=record.plan.repository.platform,
        resources=tuple(
            WorkspaceResourceRequest(path=item.path, access="write") for item in mutations
        ),
    )
    observed = {item.path: item for item in snapshot.resources}
    for mutation in mutations:
        observation = observed[mutation.path]
        if observation.kind == "missing":
            actual = WorkspaceFileVersion(presence="absent", size=0)
        elif observation.kind == "file":
            # 共用原Planner的有界原生文件读取及元数据规则，不新增沿路径读入的实现。
            body, mode = _read_existing(root, mutation.path, record.plan.repository.platform)
            actual = WorkspaceFileVersion(
                presence="file",
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                mode=mode,
            )
            if actual.sha256 != observation.content_sha256 or actual.size != observation.size:
                raise KernelError("git_checkpoint_diverged", "受管Worktree读取期间发生变化")
        else:
            raise KernelError("git_checkpoint_diverged", "受管Worktree目标不再是普通文件")
        if actual not in (mutation.before, mutation.after):
            raise KernelError("git_checkpoint_diverged", "受管Worktree目标包含第三内容")
    verify_workspace_snapshot(snapshot, root)
