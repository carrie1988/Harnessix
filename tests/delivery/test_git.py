from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import git as git_delivery
from harnessix.delivery.git import GitDeliveryRuntime
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore

_COMMIT_TIME = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
_CRASH_WORKER = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.delivery import git as git_delivery
from harnessix.delivery.git import GitDeliveryRuntime
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore
workspace_state, git_state, lease_path, executable, repository, commit_id, point = sys.argv[1:]
def crash(current):
    if current == point:
        os._exit(89)
git_delivery._fault = crash
with SQLiteWorkspaceTransactionStore(Path(workspace_state)) as workspace_store:
    with SQLiteGitDeliveryStore(Path(git_state)) as git_store:
        with WorkspaceLeaseStore(Path(lease_path)) as leases:
            runtime = GitDeliveryRuntime(workspace_store, git_store, leases, Path(executable))
            record = git_store.load_commit(UUID(commit_id))
            worktree = git_store.load_worktree(record.spec.checkpoint.worktree_id)
            lease = leases.acquire(
                worktree.plan.repository.workspace_id, 'test', ttl_seconds=60)
            runtime.commit(record.commit_id, Path(repository),
                approval_fingerprint=record.spec.fingerprint, lease=lease)
raise AssertionError('未到达Git硬退出切点')
"""


def _git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git不可用")
    return Path(executable).resolve()


def _run(root: Path, *arguments: str, input_data: bytes | None = None) -> bytes:
    environment = {
        "PATH": os.pathsep.join((str(_git().parent), os.defpath)),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment.update({"SystemRoot": system_root, "WINDIR": system_root})
    completed = subprocess.run(
        [str(_git()), *arguments],
        cwd=root,
        env=environment,
        input=input_data,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return completed.stdout


def _repository(root: Path) -> str:
    root.mkdir(parents=True)
    _run(root, "init", "-q")
    _run(root, "config", "user.name", "Harnessix Test")
    _run(root, "config", "user.email", "test@harnessix.invalid")
    _run(root, "config", "core.autocrlf", "false")
    (root / "modify.txt").write_bytes(b"before\n")
    (root / "delete.txt").write_bytes(b"delete\n")
    executable = root / "script.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    if os.name == "posix":
        executable.chmod(0o755)
    _run(root, "add", "--", "modify.txt", "delete.txt", "script.sh")
    _run(root, "commit", "-qm", "baseline")
    return _run(root, "rev-parse", "HEAD").decode().strip()


def _prepared(tmp_path: Path, *, request: str) -> tuple[Any, ...]:
    repository = tmp_path / "repository"
    baseline = _repository(repository)
    prepared = prepare_workspace_transaction(
        repository,
        {
            "modify.txt": DesiredWorkspaceFile(b"after\n", 0o644),
            "delete.txt": DesiredWorkspaceFile(None),
            "added.bin": DesiredWorkspaceFile(b"\0\x01\x02", 0o644),
        },
        request_id=request,
    )
    workspace_state = tmp_path / "state/workspace"
    git_state = tmp_path / "state/git"
    lease_path = tmp_path / "state/leases.db"
    workspace_store = SQLiteWorkspaceTransactionStore(workspace_state)
    git_store = SQLiteGitDeliveryStore(git_state)
    leases = WorkspaceLeaseStore(lease_path)
    runtime = GitDeliveryRuntime(workspace_store, git_store, leases, _git())
    transaction = workspace_store.save(prepared)
    lease = leases.acquire(transaction.plan.source.workspace_id, "test", ttl_seconds=60)
    return (
        repository,
        baseline,
        workspace_state,
        git_state,
        lease_path,
        workspace_store,
        git_store,
        leases,
        runtime,
        transaction,
        lease,
    )


def _checkpoint(tmp_path: Path, *, request: str = "git") -> tuple[Any, ...]:
    values = _prepared(tmp_path, request=request)
    repository, baseline, *_, runtime, transaction, lease = values
    planned = runtime.plan_worktree(transaction.transaction_id, repository)
    ready = runtime.create_worktree(
        planned.worktree_id,
        repository,
        approval_fingerprint=transaction.plan.fingerprint,
        lease=lease,
    )
    checkpoint = runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
    return (*values, ready, checkpoint)


def _close(values: tuple[object, ...]) -> None:
    for value in values:
        if isinstance(
            value, WorkspaceLeaseStore | SQLiteGitDeliveryStore | SQLiteWorkspaceTransactionStore
        ):
            value.close()


def test_managed_worktree_checkpoint_and_commit_preserve_source(tmp_path: Path) -> None:
    values = _checkpoint(tmp_path)
    (
        repository,
        baseline,
        _workspace_state,
        _git_state,
        _lease_path,
        workspace_store,
        git_store,
        leases,
        runtime,
        _transaction,
        lease,
        ready,
        checkpoint,
    ) = values
    try:
        assert checkpoint.base_commit_oid == baseline
        assert checkpoint.tree_oid != checkpoint.base_tree_oid
        worktree = Path(ready.plan.path)
        assert (worktree / "modify.txt").read_bytes() == b"after\n"
        assert not (worktree / "delete.txt").exists()
        assert (worktree / "added.bin").read_bytes() == b"\0\x01\x02"
        assert _run(repository, "status", "--porcelain=v2", "-z") == b""
        assert _run(repository, "rev-parse", "HEAD").decode().strip() == baseline

        planned = runtime.plan_commit(
            checkpoint.checkpoint_id,
            repository,
            branch="harnessix/transaction-test",
            author_name="Harnessix Agent",
            author_email="agent@harnessix.invalid",
            message="Apply transaction",
            authored_at=_COMMIT_TIME,
        )
        committed = runtime.commit(
            planned.commit_id,
            repository,
            approval_fingerprint=planned.spec.fingerprint,
            lease=lease,
        )
        repeated = runtime.commit(
            planned.commit_id,
            repository,
            approval_fingerprint=planned.spec.fingerprint,
            lease=lease,
        )
        commit_tree = (
            _run(repository, "rev-parse", "refs/heads/harnessix/transaction-test^{tree}")
            .decode()
            .strip()
        )
        parent = (
            _run(repository, "rev-parse", "refs/heads/harnessix/transaction-test^").decode().strip()
        )
    finally:
        _close((leases, git_store, workspace_store))
    assert committed == repeated and committed.state == "committed"
    assert committed.commit_oid == planned.spec.expected_commit_oid
    assert commit_tree == checkpoint.tree_oid and parent == baseline
    assert _run(repository, "rev-parse", "HEAD").decode().strip() == baseline
    assert _run(repository, "status", "--porcelain=v2", "-z") == b""


def test_dirty_repository_and_configured_filter_fail_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    prepared = prepare_workspace_transaction(
        repository,
        {"modify.txt": DesiredWorkspaceFile(b"after\n", 0o644)},
        request_id="unsafe",
    )
    with SQLiteWorkspaceTransactionStore(tmp_path / "state/workspace") as workspace_store:
        with SQLiteGitDeliveryStore(tmp_path / "state/git") as git_store:
            with WorkspaceLeaseStore(tmp_path / "state/leases.db") as leases:
                runtime = GitDeliveryRuntime(workspace_store, git_store, leases, _git())
                transaction = workspace_store.save(prepared)
                (repository / "untracked.txt").write_bytes(b"dirty")
                with pytest.raises(KernelError) as dirty:
                    runtime.plan_worktree(transaction.transaction_id, repository)
                (repository / "untracked.txt").unlink()
                _run(repository, "config", "filter.hostile.clean", "false")
                with pytest.raises(KernelError) as configured:
                    runtime.plan_worktree(transaction.transaction_id, repository)
    assert dirty.value.code == "delivery_dirty_conflict"
    assert configured.value.code == "git_filter_unsupported"


@pytest.mark.parametrize(
    ("unsafe", "expected_code"),
    [
        ("include", "git_config_unsupported"),
        ("attributes", "git_attributes_unsupported"),
        ("gitmodules", "git_tree_unsupported"),
        ("sparse", "git_sparse_checkout_unsupported"),
        ("alternates", "git_alternates_unsupported"),
    ],
)
def test_git_repository_control_planes_fail_closed(
    tmp_path: Path, unsafe: str, expected_code: str
) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    if unsafe == "include":
        external = tmp_path / "external.config"
        external.write_text("[core]\n\tignorecase = false\n", encoding="utf-8")
        _run(repository, "config", "include.path", str(external))
    elif unsafe == "attributes":
        (repository / ".gitattributes").write_text("*.txt filter=lfs\n", encoding="utf-8")
        _run(repository, "add", ".gitattributes")
        _run(repository, "commit", "-qm", "attributes")
    elif unsafe == "gitmodules":
        (repository / ".gitmodules").write_text(
            '[submodule "unsafe"]\n\tpath = unsafe\n\turl = ../unsafe\n', encoding="utf-8"
        )
        _run(repository, "add", ".gitmodules")
        _run(repository, "commit", "-qm", "gitmodules")
    elif unsafe == "sparse":
        _run(repository, "config", "core.sparseCheckout", "true")
    else:
        alternate = tmp_path / "alternate-objects"
        alternate.mkdir()
        info = repository / ".git/objects/info"
        info.mkdir(exist_ok=True)
        (info / "alternates").write_text(str(alternate), encoding="utf-8")
    prepared = prepare_workspace_transaction(
        repository,
        {"modify.txt": DesiredWorkspaceFile(b"after\n", 0o644)},
        request_id=f"unsafe-{unsafe}",
    )
    with SQLiteWorkspaceTransactionStore(tmp_path / "state/workspace") as workspace_store:
        with SQLiteGitDeliveryStore(tmp_path / "state/git") as git_store:
            with WorkspaceLeaseStore(tmp_path / "state/leases.db") as leases:
                runtime = GitDeliveryRuntime(workspace_store, git_store, leases, _git())
                transaction = workspace_store.save(prepared)
                with pytest.raises(KernelError) as denied:
                    runtime.plan_worktree(transaction.transaction_id, repository)
    assert denied.value.code == expected_code


def test_worktree_registration_crash_reconciles_and_source_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _prepared(tmp_path, request="worktree-crash")
    repository, _baseline, *_, workspace_store, git_store, leases, runtime, transaction, lease = (
        values
    )
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)

        def crash(point: str) -> None:
            if point == "worktree_registered":
                raise RuntimeError("crash")

        monkeypatch.setattr(git_delivery, "_fault", crash)
        with pytest.raises(RuntimeError):
            runtime.create_worktree(
                planned.worktree_id,
                repository,
                approval_fingerprint=transaction.plan.fingerprint,
                lease=lease,
            )
        monkeypatch.setattr(git_delivery, "_fault", lambda _: None)
        recovered = runtime.reconcile_worktree(planned.worktree_id, repository)
        assert recovered.state == "ready"

        second = prepare_workspace_transaction(
            repository,
            {"modify.txt": DesiredWorkspaceFile(b"other\n", 0o644)},
            request_id="source-drift",
        )
        second_record = workspace_store.save(second)
        second_plan = runtime.plan_worktree(second_record.transaction_id, repository)
        (repository / "modify.txt").write_bytes(b"user-change\n")
        with pytest.raises(KernelError) as changed:
            runtime.create_worktree(
                second_plan.worktree_id,
                repository,
                approval_fingerprint=second_record.plan.fingerprint,
                lease=lease,
            )
    finally:
        _close((leases, git_store, workspace_store))
    assert changed.value.code in {"delivery_dirty_conflict", "git_repository_changed"}


@pytest.mark.parametrize(
    ("point", "expected_state"),
    [("commit_object_written", "interrupted"), ("commit_ref_updated", "committed")],
)
def test_real_process_exit_reconciles_commit_without_duplicate(
    tmp_path: Path, point: str, expected_state: str
) -> None:
    values = _checkpoint(tmp_path, request=f"crash-{point}")
    (
        repository,
        _baseline,
        workspace_state,
        git_state,
        lease_path,
        workspace_store,
        git_store,
        leases,
        runtime,
        _transaction,
        _lease,
        _ready,
        checkpoint,
    ) = values
    branch = "harnessix/crash-object" if point == "commit_object_written" else "harnessix/crash-ref"
    planned = runtime.plan_commit(
        checkpoint.checkpoint_id,
        repository,
        branch=branch,
        author_name="Harnessix Agent",
        author_email="agent@harnessix.invalid",
        message="Crash recovery",
        authored_at=_COMMIT_TIME,
    )
    _close((leases, git_store, workspace_store))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_WORKER,
            str(workspace_state),
            str(git_state),
            str(lease_path),
            str(_git()),
            str(repository),
            str(planned.commit_id),
            point,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 89, completed.stderr
    with SQLiteWorkspaceTransactionStore(workspace_state) as reopened_workspace:
        with SQLiteGitDeliveryStore(git_state) as reopened_git:
            with WorkspaceLeaseStore(lease_path) as reopened_leases:
                reopened = GitDeliveryRuntime(
                    reopened_workspace, reopened_git, reopened_leases, _git()
                )
                reconciled = reopened.reconcile_commit(planned.commit_id, repository)
                assert reconciled.state == expected_state
                lease = reopened_leases.acquire(
                    reopened_git.load_worktree(checkpoint.worktree_id).plan.repository.workspace_id,
                    "test",
                    ttl_seconds=60,
                )
                committed = reopened.commit(
                    planned.commit_id,
                    repository,
                    approval_fingerprint=planned.spec.fingerprint,
                    lease=lease,
                )
    assert committed.state == "committed"
    assert (
        _run(repository, "rev-parse", planned.spec.branch_ref).decode().strip()
        == planned.spec.expected_commit_oid
    )


def test_commit_approval_and_existing_branch_are_rejected(tmp_path: Path) -> None:
    values = _checkpoint(tmp_path, request="approval")
    (
        repository,
        _baseline,
        *_,
        workspace_store,
        git_store,
        leases,
        runtime,
        _transaction,
        lease,
        _ready,
        checkpoint,
    ) = values
    try:
        _run(repository, "branch", "occupied")
        with pytest.raises(KernelError) as occupied:
            runtime.plan_commit(
                checkpoint.checkpoint_id,
                repository,
                branch="occupied",
                author_name="Harnessix Agent",
                author_email="agent@harnessix.invalid",
                message="Denied",
                authored_at=_COMMIT_TIME,
            )
        planned = runtime.plan_commit(
            checkpoint.checkpoint_id,
            repository,
            branch="harnessix/approval",
            author_name="Harnessix Agent",
            author_email="agent@harnessix.invalid",
            message="Approved only by fingerprint",
            authored_at=_COMMIT_TIME,
        )
        with pytest.raises(KernelError) as approval:
            runtime.commit(
                planned.commit_id,
                repository,
                approval_fingerprint="f" * 64,
                lease=lease,
            )
    finally:
        _close((leases, git_store, workspace_store))
    assert occupied.value.code == "git_branch_exists"
    assert approval.value.code == "git_commit_approval_mismatch"
    result = subprocess.run(
        [str(_git()), "rev-parse", "--verify", planned.spec.branch_ref],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_git_store_rejects_unknown_schema_and_corrupt_payload(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    database = sqlite3.connect(unknown / "git-delivery.db")
    database.execute(
        "CREATE TABLE git_delivery_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO git_delivery_metadata VALUES ('schema_version', '2')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteGitDeliveryStore(unknown)

    values = _prepared(tmp_path / "corrupt", request="corrupt")
    repository, _baseline, *_, workspace_store, git_store, leases, runtime, transaction, _lease = (
        values
    )
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)
        git_store._db.execute(  # noqa: SLF001 - 故障注入验证持久化边界
            "UPDATE git_worktrees SET payload='{}' WHERE worktree_id=?",
            (str(planned.worktree_id),),
        )
        with pytest.raises(KernelError) as corrupt:
            git_store.load_worktree(planned.worktree_id)
    finally:
        _close((leases, git_store, workspace_store))
    assert version.value.code == "git_delivery_store_version"
    assert corrupt.value.code == "git_delivery_store_corrupt"
