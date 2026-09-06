"""通过既有固定 Git 读取端口采集 Eval 工作区证据。"""

from __future__ import annotations

from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.evals.contracts import EvalGitEvidence
from harnessix.tools.git import GitReadRuntime
from harnessix.tools.git_contracts import (
    GitDiffInput,
    GitDiffOutput,
    GitStatusInput,
    GitStatusOutput,
)


async def collect_git_evidence(
    root: Path,
    git_executable: Path,
    *,
    baseline_revision: str,
    baseline_tree_sha256: str,
) -> EvalGitEvidence:
    """读取完整状态和工作区 Diff 摘要；状态超过上限时拒绝评分。"""

    runtime = GitReadRuntime(root, git_executable)
    cancel = CancelToken()
    status = await runtime.execute(GitStatusInput(limit=200), cancel)
    diff = await runtime.execute(GitDiffInput(target="worktree", context_lines=3), cancel)
    if not isinstance(status, GitStatusOutput) or not isinstance(diff, GitDiffOutput):
        raise KernelError("eval_git_evidence_invalid", "Git 证据端口返回了错误类型")
    if status.truncated or len(status.entries) != status.total_entries:
        raise KernelError("eval_git_status_incomplete", "Git 变更超过 Eval 可评分上限")
    if status.head_oid is None:
        raise KernelError("eval_git_head_missing", "Eval 仓库缺少已提交基线")

    changed: set[str] = set()
    staged: set[str] = set()
    untracked: set[str] = set()
    unsupported: set[str] = set()
    for entry in status.entries:
        paths = {entry.path}
        if entry.original_path is not None:
            paths.add(entry.original_path)
        changed.update(paths)
        if entry.index_status not in {".", "?"}:
            staged.update(paths)
        if entry.kind == "untracked":
            untracked.update(paths)
        if entry.kind != "ordinary":
            unsupported.update(paths)
    return EvalGitEvidence(
        baseline_revision=baseline_revision,
        baseline_tree_sha256=baseline_tree_sha256,
        head_revision=status.head_oid,
        changed_paths=tuple(sorted(changed)),
        staged_paths=tuple(sorted(staged)),
        untracked_paths=tuple(sorted(untracked)),
        unsupported_change_paths=tuple(sorted(unsupported)),
        status_sha256=status.revision,
        diff_sha256=diff.observed_sha256,
        diff_observed_bytes=diff.observed_bytes,
    )
