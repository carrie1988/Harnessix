"""Git检查点不得覆盖受管Worktree中的第三内容或在失去租约后物化。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from tests.delivery.test_git import _close, _prepared, _run


@pytest.mark.parametrize(
    ("member", "staged"),
    [
        ("modify.txt", False),
        ("delete.txt", False),
        ("added.bin", False),
        ("script.sh", False),
        ("script.sh", True),
    ],
)
def test_checkpoint_refuses_third_content_without_altering_worktree(tmp_path, member, staged):
    values = _prepared(tmp_path, request="checkpoint-user-change")
    repository, baseline, *_, runtime, transaction, lease = values
    workspace_store, git_store, leases = values[5:8]
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)
        ready = runtime.create_worktree(
            planned.worktree_id,
            repository,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        worktree = Path(ready.plan.path)
        user_body = b"user-content-after-plan\n"
        (worktree / member).write_bytes(user_body)
        if staged:
            _run(worktree, "add", "--", member)
        original_index = _run(worktree, "write-tree")
        original_status = _run(worktree, "status", "--porcelain=v2", "-z")
        with pytest.raises(KernelError) as caught:
            runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
        assert caught.value.code == "git_checkpoint_diverged"
        assert (worktree / member).read_bytes() == user_body
        assert _run(worktree, "write-tree") == original_index
        assert _run(worktree, "status", "--porcelain=v2", "-z") == original_status
        assert git_store.checkpoint_for_worktree(ready.worktree_id) is None
        assert _run(repository, "rev-parse", "HEAD").decode().strip() == baseline
        assert _run(repository, "status", "--porcelain=v2", "-z") == b""
    finally:
        _close((leases, git_store, workspace_store))


def test_checkpoint_rechecks_lease_before_worktree_materialization(tmp_path, monkeypatch):
    values = _prepared(tmp_path, request="checkpoint-lease-lost")
    repository, *_, runtime, transaction, lease = values
    workspace_store, git_store, leases = values[5:8]
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)
        ready = runtime.create_worktree(
            planned.worktree_id,
            repository,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        worktree = Path(ready.plan.path)
        original = (worktree / "modify.txt").read_bytes()
        original_run = runtime._git.run
        lost = []

        def lose_before_materialization(cwd, arguments, **kwargs):
            result = original_run(cwd, arguments, **kwargs)
            if arguments[0] == "diff-tree" and not lost:
                leases.release(lease)
                lost.append(True)
            return result

        monkeypatch.setattr(runtime._git, "run", lose_before_materialization)
        with pytest.raises(KernelError) as caught:
            runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
        assert caught.value.code == "workspace_lease_lost"
        assert lost == [True]
        assert (worktree / "modify.txt").read_bytes() == original
        assert git_store.checkpoint_for_worktree(ready.worktree_id) is None
    finally:
        _close((leases, git_store, workspace_store))


def test_checkpoint_keeps_unrelated_untracked_file_and_known_after_members(tmp_path):
    values = _prepared(tmp_path, request="checkpoint-known-images")
    repository, *_, runtime, transaction, lease = values
    workspace_store, git_store, leases = values[5:8]
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)
        ready = runtime.create_worktree(
            planned.worktree_id,
            repository,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        worktree = Path(ready.plan.path)
        (worktree / "modify.txt").write_bytes(b"after\n")
        (worktree / "delete.txt").unlink()
        (worktree / "note.txt").write_bytes(b"unrelated-user-note\n")
        checkpoint = runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
        assert checkpoint.worktree_id == ready.worktree_id
        assert (worktree / "note.txt").read_bytes() == b"unrelated-user-note\n"
        assert (worktree / "added.bin").read_bytes() == b"\0\x01\x02"
        assert _run(repository, "status", "--porcelain=v2", "-z") == b""
    finally:
        _close((leases, git_store, workspace_store))


def test_checkpoint_reconstructs_exact_materialization_after_save_failure(tmp_path, monkeypatch):
    values = _prepared(tmp_path, request="checkpoint-save-loss")
    repository, *_, runtime, transaction, lease = values
    workspace_store, git_store, leases = values[5:8]
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)
        ready = runtime.create_worktree(
            planned.worktree_id,
            repository,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        worktree = Path(ready.plan.path)
        original_save = git_store.save_checkpoint

        def fail_before_save(_checkpoint):
            raise RuntimeError("controlled-save-failure")

        monkeypatch.setattr(git_store, "save_checkpoint", fail_before_save)
        with pytest.raises(RuntimeError, match="controlled-save-failure"):
            runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
        assert git_store.checkpoint_for_worktree(ready.worktree_id) is None
        assert (worktree / "modify.txt").read_bytes() == b"after\n"
        previous_index = _run(worktree, "write-tree")
        monkeypatch.setattr(git_store, "save_checkpoint", original_save)
        checkpoint = runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
        assert checkpoint.tree_oid == previous_index.decode().strip()
        assert runtime.create_checkpoint(ready.worktree_id, repository, lease=lease) == checkpoint
        assert _run(repository, "status", "--porcelain=v2", "-z") == b""
    finally:
        _close((leases, git_store, workspace_store))


def test_git_delivery_evidence_includes_checkpoint_guard_source(monkeypatch):
    from harnessix.delivery.git import git_delivery_implementation_digest

    original = Path.read_bytes
    before = git_delivery_implementation_digest()
    observed = []

    def changed_source(path):
        body = original(path)
        if path.name == "git_checkpoint.py":
            observed.append(path.name)
            return body + b"\n# controlled-source-change\n"
        return body

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    assert git_delivery_implementation_digest() != before
    assert observed == ["git_checkpoint.py"]
