"""真实私有来源的领域端口验证；不代表产品 Bridge 认证或默认装配。"""

from __future__ import annotations

import inspect
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceTransactionRecord
from harnessix.delivery.git import GitDeliveryRuntime, git_delivery_implementation_digest
from harnessix.delivery.git_contracts import GitRepositoryBinding, ManagedGitWorktreeRecord
from harnessix.delivery.git_identity import _path_text
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.contracts import WorkspaceLease
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot import verify_workspace_snapshot
from tests.delivery.test_git import _close, _git, _prepared, _repository, _run
from tests.delivery.test_git_projection_ordering import _linked_anchor

PrivateSource = tuple[tuple[Any, ...], Path, WorkspaceTransactionRecord, WorkspaceLease]


@pytest.fixture
def private_source(tmp_path: Path) -> Iterator[PrivateSource]:
    """复用原真实 Git 夹具，保留独立 A 的原 Planner、Store 和 Lease。"""
    with _linked_anchor(tmp_path) as source:
        yield source


def _runtime(
    values: tuple[Any, ...], resolver: Callable[[GitRepositoryBinding], Path]
) -> GitDeliveryRuntime:
    return GitDeliveryRuntime(*values[5:8], _git(), source_resolver=resolver)


def _ready(source: PrivateSource, runtime: GitDeliveryRuntime) -> ManagedGitWorktreeRecord:
    _, anchor, transaction, lease = source
    planned = runtime.plan_worktree(transaction.transaction_id, anchor)
    return runtime.create_worktree(
        planned.worktree_id,
        anchor,
        approval_fingerprint=transaction.plan.fingerprint,
        lease=lease,
    )


def _admin(root: Path) -> Path:
    raw = _run(root, "rev-parse", "--git-dir").decode().strip()
    path = Path(raw)
    return (path if path.is_absolute() else root / path).resolve(strict=True)


def test_private_detached_source_materializes_checkpoint_without_publishing(
    private_source: PrivateSource,
) -> None:
    values, anchor, transaction, lease = private_source
    user, baseline = values[:2]
    store, git_store = values[5:7]
    observed: list[GitRepositoryBinding] = []

    def resolve(binding: GitRepositoryBinding) -> Path:
        observed.append(binding)
        return anchor

    runtime = _runtime(values, resolve)
    assert _run(anchor, "rev-parse", "--abbrev-ref", "HEAD") == b"HEAD\n"
    ready = _ready(private_source, runtime)
    assert observed == [ready.plan.repository]
    delivery = Path(ready.plan.path)
    assert (delivery / "modify.txt").read_bytes() == b"before\n"
    checkpoint = runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease)
    assert checkpoint.base_commit_oid == baseline
    assert checkpoint.tree_oid != checkpoint.base_tree_oid
    assert (delivery / "modify.txt").read_bytes() == b"after\n"
    assert not (delivery / "delete.txt").exists()
    assert (delivery / "added.bin").read_bytes() == b"\0\x01\x02"
    assert _run(delivery, "write-tree").decode().strip() == checkpoint.tree_oid
    assert git_store.checkpoint_for_worktree(ready.worktree_id) == checkpoint
    assert runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease) == checkpoint
    assert observed == [ready.plan.repository] * 3
    assert store.load(transaction.transaction_id) == transaction
    assert transaction.state == "prepared"
    assert verify_workspace_snapshot(transaction.plan.source, anchor) == transaction.plan.source
    assert (anchor / "modify.txt").read_bytes() == b"before\n"
    assert (anchor / "delete.txt").read_bytes() == b"delete\n"
    assert not (anchor / "added.bin").exists()
    for root in (anchor, user):
        assert _run(root, "status", "--porcelain=v2", "--untracked-files=all", "-z") == b""
        assert _run(root, "rev-parse", "HEAD").decode().strip() == baseline


def test_private_source_reopen_requires_explicit_port_reassembly(
    private_source: PrivateSource,
) -> None:
    values, anchor, transaction, lease = private_source
    ready = _ready(private_source, _runtime(values, lambda _: anchor))
    with SQLiteWorkspaceTransactionStore(values[2]) as store:
        with SQLiteGitDeliveryStore(values[3]) as git_store:
            with WorkspaceLeaseStore(values[4]) as leases:
                assert git_store.load_worktree(ready.worktree_id) == ready
                assert store.load(transaction.transaction_id) == transaction
                unassembled = GitDeliveryRuntime(store, git_store, leases, _git())
                with pytest.raises(KernelError) as rejected:
                    unassembled.create_checkpoint(ready.worktree_id, anchor, lease=lease)
                assert rejected.value.code == "git_worktree_binding_invalid"
                assert git_store.checkpoint_for_worktree(ready.worktree_id) is None
                assembled = GitDeliveryRuntime(
                    store, git_store, leases, _git(), source_resolver=lambda _: anchor
                )
                checkpoint = assembled.create_checkpoint(ready.worktree_id, anchor, lease=lease)
                assert checkpoint.worktree_id == ready.worktree_id
                assert store.load(transaction.transaction_id) == transaction
                assert _run(anchor, "status", "--porcelain=v2", "-z") == b""


@pytest.mark.parametrize("choice", ["raise", "missing", "wrong", "relative", "non_root"])
def test_explicit_resolver_failure_never_uses_ordinary_adjacent_source(
    tmp_path: Path,
    choice: str,
) -> None:
    """普通邻接来源本可成功，但显式端口的失败不能触发旧候选解析。"""
    values = _prepared(tmp_path, request="explicit-no-fallback")
    repository, *_, transaction, lease = values
    calls: list[GitRepositoryBinding] = []
    wrong = tmp_path / "wrong"
    if choice == "wrong":
        _repository(wrong)

    def resolve(binding: GitRepositoryBinding) -> Path:
        calls.append(binding)
        if choice == "raise":
            raise KernelError("git_repository_changed", "受信宿主未登记来源")
        if choice == "relative":
            return Path("repository")
        if choice == "non_root":
            return repository / ".git"
        return wrong if choice == "wrong" else tmp_path / "missing"

    runtime = _runtime(values, resolve)
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, repository)
        with pytest.raises(KernelError) as rejected:
            runtime.create_worktree(
                planned.worktree_id,
                repository,
                approval_fingerprint=transaction.plan.fingerprint,
                lease=lease,
            )
        assert rejected.value.code == "git_worktree_binding_invalid"
        assert calls == [planned.plan.repository]
        actual = values[6].load_worktree(planned.worktree_id)
        assert actual.state == "creating" and actual.binding is None
        assert Path(actual.plan.path).is_dir()
        assert values[6].checkpoint_for_worktree(planned.worktree_id) is None
        assert values[5].load(transaction.transaction_id) == transaction
        assert _run(repository, "status", "--porcelain=v2", "-z") == b""
    finally:
        values[7].release(lease)
        _close(values)


def test_ordinary_source_accepts_explicit_resolver(tmp_path: Path) -> None:
    values = _prepared(tmp_path, request="ordinary-explicit-source")
    repository, *_, transaction, lease = values
    runtime = _runtime(values, lambda _: repository)
    try:
        ready = _ready((values, repository, transaction, lease), runtime)
        checkpoint = runtime.create_checkpoint(ready.worktree_id, repository, lease=lease)
        assert checkpoint.worktree_id == ready.worktree_id
        assert values[5].load(transaction.transaction_id) == transaction
        assert _run(repository, "status", "--porcelain=v2", "-z") == b""
    finally:
        values[7].release(lease)
        _close(values)


def test_wrong_source_root_rejected_even_with_shared_common_and_same_head(
    private_source: PrivateSource,
) -> None:
    values, anchor, transaction, lease = private_source
    user = values[0]
    runtime = _runtime(values, lambda _: user)
    planned = runtime.plan_worktree(transaction.transaction_id, anchor)
    with pytest.raises(KernelError) as rejected:
        runtime.create_worktree(
            planned.worktree_id,
            anchor,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
    assert rejected.value.code == "git_worktree_binding_invalid"
    actual = values[6].load_worktree(planned.worktree_id)
    with pytest.raises(KernelError) as root:
        runtime._repository_root_from_binding(planned.plan.repository, Path(actual.plan.path))
    assert root.value.code == "git_repository_changed"
    assert actual.state == "creating" and actual.binding is None
    assert values[5].load(transaction.transaction_id) == transaction
    assert _run(anchor, "status", "--porcelain=v2", "-z") == b""


@pytest.mark.parametrize(
    ("drift", "expected"),
    [
        ("untracked", "delivery_dirty_conflict"),
        ("tracked", "delivery_dirty_conflict"),
        ("head", "git_repository_changed"),
        ("config", "git_repository_changed"),
        ("root_identity", "git_repository_changed"),
        ("backlink", "git_worktree_binding_invalid"),
        ("admin", "git_worktree_binding_invalid"),
        ("unregistered", "git_repository_invalid"),
    ],
)
def test_private_source_drift_rejected_before_checkpoint_materialization(
    private_source: PrivateSource,
    drift: str,
    expected: str,
) -> None:
    values, anchor, transaction, lease = private_source
    user, baseline = values[:2]
    runtime = _runtime(values, lambda _: anchor)
    ready = _ready(private_source, runtime)
    delivery = Path(ready.plan.path)
    before = _run(delivery, "write-tree")
    admin = _admin(anchor)
    if drift == "untracked":
        (anchor / "note.txt").write_bytes(b"private change\n")
    elif drift == "tracked":
        (anchor / "modify.txt").write_bytes(b"private change\n")
    elif drift == "head":
        tree = _run(anchor, "rev-parse", "HEAD^{tree}").decode().strip()
        head = _run(anchor, "commit-tree", tree, "-p", baseline, input_data=b"new head\n")
        _run(anchor, "update-ref", "HEAD", head.decode().strip(), baseline)
        assert _run(anchor, "status", "--porcelain=v2", "-z") == b""
    elif drift == "config":
        _run(anchor, "config", "delivery.privateSource", "changed")
    elif drift == "root_identity":
        moved = anchor.with_name("old-source")
        anchor.rename(moved)
        shutil.copytree(moved, anchor)
        assert _run(anchor, "status", "--porcelain=v2", "-z") == b""
    elif drift == "backlink":
        (admin / "gitdir").write_text(str(delivery / ".git") + "\n", encoding="utf-8")
        assert runtime.bind_repository(anchor, transaction.plan.source.workspace_id) == (
            ready.plan.repository
        )
    elif drift == "admin":
        common = runtime._common_directory(anchor)
        moved = common / "foreign-admin"
        admin.rename(moved)
        (moved / "commondir").write_text(str(common) + "\n", encoding="utf-8")
        (anchor / ".git").write_text("gitdir: " + str(moved) + "\n", encoding="utf-8")
        assert runtime.bind_repository(anchor, transaction.plan.source.workspace_id) == (
            ready.plan.repository
        )
    else:
        _run(user, "worktree", "remove", "--force", str(anchor))
    with pytest.raises(KernelError) as resolved:
        runtime._repository_root_from_binding(ready.plan.repository, delivery)
    assert resolved.value.code == expected
    with pytest.raises(KernelError) as rejected:
        runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease)
    assert rejected.value.code == "git_worktree_binding_invalid"
    assert _run(delivery, "write-tree") == before
    assert (delivery / "modify.txt").read_bytes() == b"before\n"
    assert values[6].checkpoint_for_worktree(ready.worktree_id) is None
    assert values[5].load(transaction.transaction_id) == transaction


@pytest.mark.skipif(os.name != "posix", reason="需要原生符号链接")
@pytest.mark.parametrize("member", ["gitfile", "admin", "commondir", "backlink"])
def test_private_source_symlink_control_files_rejected(
    private_source: PrivateSource,
    member: str,
) -> None:
    values, anchor, transaction, lease = private_source
    runtime = _runtime(values, lambda _: anchor)
    ready = _ready(private_source, runtime)
    admin = _admin(anchor)
    path = {
        "gitfile": anchor / ".git",
        "admin": admin,
        "commondir": admin / "commondir",
        "backlink": admin / "gitdir",
    }[member]
    moved = (
        anchor.parent / "original-gitfile"
        if member == "gitfile"
        else path.with_name(path.name + "-original")
    )
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=member == "admin")
    # 原绑定保持一致时，也必须独立拒绝来源控制文件的符号链接。
    assert runtime.bind_repository(anchor, transaction.plan.source.workspace_id) == (
        ready.plan.repository
    )
    with pytest.raises(KernelError) as rejected:
        runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease)
    assert rejected.value.code == "git_worktree_binding_invalid"
    assert values[6].checkpoint_for_worktree(ready.worktree_id) is None
    assert (Path(ready.plan.path) / "modify.txt").read_bytes() == b"before\n"


def test_source_registration_is_required_independently_of_binding_and_backlink(
    private_source: PrivateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, anchor, transaction, lease = private_source
    runtime = _runtime(values, lambda _: anchor)
    ready = _ready(private_source, runtime)
    registered = runtime._registered_worktrees(anchor)
    source_path = _path_text(anchor)
    assert source_path in registered
    monkeypatch.setattr(runtime, "_registered_worktrees", lambda _: registered - {source_path})
    assert runtime.bind_repository(anchor, transaction.plan.source.workspace_id) == (
        ready.plan.repository
    )
    with pytest.raises(KernelError) as rejected:
        runtime._repository_root_from_binding(ready.plan.repository, Path(ready.plan.path))
    assert rejected.value.code == "git_repository_changed"
    with pytest.raises(KernelError):
        runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease)
    assert values[6].checkpoint_for_worktree(ready.worktree_id) is None


def test_resolved_source_and_delivery_require_same_real_common_directory(
    private_source: PrivateSource,
    tmp_path: Path,
) -> None:
    values, anchor, transaction, _lease = private_source
    runtime = _runtime(values, lambda _: anchor)
    binding = runtime.bind_repository(anchor, transaction.plan.source.workspace_id)
    other = tmp_path / "independent"
    _repository(other)
    with pytest.raises(KernelError) as rejected:
        runtime._repository_root_from_binding(binding, other)
    assert rejected.value.code == "git_repository_changed"
    assert values[5].load(transaction.transaction_id) == transaction


def test_delivery_backlink_validation_is_preserved(private_source: PrivateSource) -> None:
    values, anchor, _transaction, lease = private_source
    runtime = _runtime(values, lambda _: anchor)
    ready = _ready(private_source, runtime)
    delivery = Path(ready.plan.path)
    (_admin(delivery) / "gitdir").write_text(str(anchor / ".git") + "\n", encoding="utf-8")
    with pytest.raises(KernelError) as rejected:
        runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease)
    assert rejected.value.code == "git_worktree_binding_invalid"
    assert values[6].checkpoint_for_worktree(ready.worktree_id) is None
    assert (delivery / "modify.txt").read_bytes() == b"before\n"


@pytest.mark.parametrize(
    ("guard", "expected"),
    [
        ("approval", "git_worktree_approval_mismatch"),
        ("wrong_root", "git_repository_changed"),
        ("wrong_lease", "workspace_lease_lost"),
        ("expired_lease", "workspace_lease_lost"),
    ],
)
def test_source_port_does_not_bypass_approval_or_source_lease(
    private_source: PrivateSource,
    guard: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, anchor, transaction, lease = private_source
    calls: list[GitRepositoryBinding] = []

    def resolve(binding: GitRepositoryBinding) -> Path:
        calls.append(binding)
        return anchor

    runtime = _runtime(values, resolve)
    planned = runtime.plan_worktree(transaction.transaction_id, anchor)
    if guard == "expired_lease":
        clock = values[7]._clock
        monkeypatch.setattr(values[7], "_clock", lambda: clock() + 120)
    with pytest.raises(KernelError) as rejected:
        runtime.create_worktree(
            planned.worktree_id,
            values[0] if guard == "wrong_root" else anchor,
            approval_fingerprint="0" * 64 if guard == "approval" else transaction.plan.fingerprint,
            lease=values[-1] if guard == "wrong_lease" else lease,
        )
    assert rejected.value.code == expected
    assert not calls
    assert values[6].load_worktree(planned.worktree_id) == planned
    assert not Path(planned.plan.path).exists()


def test_source_port_is_keyword_only_and_helper_is_bound_to_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameter = inspect.signature(GitDeliveryRuntime).parameters["source_resolver"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    before = git_delivery_implementation_digest()
    original = Path.read_bytes
    observed: list[str] = []

    def changed_source(path: Path) -> bytes:
        body = original(path)
        if path.name == "git_source.py":
            observed.append(path.name)
            return body + "\n# 来源实现校验测试\n".encode()
        return body

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    assert git_delivery_implementation_digest() != before
    assert observed == ["git_source.py"]


def test_helper_change_invalidates_old_binding_and_original_approval(
    private_source: PrivateSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, anchor, transaction, lease = private_source
    runtime = _runtime(values, lambda _: anchor)
    ready = _ready(private_source, runtime)
    original = Path.read_bytes

    def changed_source(path: Path) -> bytes:
        body = original(path)
        return body + b"\n" if path.name == "git_source.py" else body

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    with pytest.raises(KernelError) as rejected:
        runtime.create_worktree(
            ready.worktree_id,
            anchor,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
    assert rejected.value.code == "git_repository_changed"
    with pytest.raises(KernelError) as checkpoint:
        runtime.create_checkpoint(ready.worktree_id, anchor, lease=lease)
    assert checkpoint.value.code == "git_worktree_binding_invalid"
    assert values[6].load_worktree(ready.worktree_id) == ready
    assert values[6].checkpoint_for_worktree(ready.worktree_id) is None
    assert values[5].load(transaction.transaction_id) == transaction
    assert (Path(ready.plan.path) / "modify.txt").read_bytes() == b"before\n"
