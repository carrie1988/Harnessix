"""用原领域端口验证派生事务顺序；不修改产品 Bridge 或授权合同。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.workspace.snapshot import verify_workspace_snapshot
from tests.delivery.test_git import _checkpoint, _close, _prepared, _run


def _record(name: str, **facts: object) -> None:
    """仅显式验证宿主保存临时夹具事实；普通回归不写额外文件。"""
    target = os.environ.get("HARNESSIX_PROJECTION_ORDERING_EVIDENCE")
    if target is None:
        return
    path = Path(target) / f"{name}.json"
    with path.open("x", encoding="utf-8") as output:
        json.dump(facts, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
    path.chmod(0o600)


@contextmanager
def _linked_anchor(tmp_path: Path) -> Iterator[tuple[tuple[Any, ...], Path, Any, Any]]:
    """复用原 CAS/Store 夹具，新建真正的 detached A 和 A 自身的事务/Lease。"""
    values = _prepared(tmp_path, request="original-source")
    user, baseline, *_paths, store, _git_store, leases, _runtime, original, original_lease = values
    anchor = tmp_path / "private/anchors/a"
    anchor.parent.mkdir(parents=True)
    anchor_lease = None
    try:
        _run(user, "worktree", "add", "--detach", str(anchor), baseline)
        desired = {
            mutation.path: DesiredWorkspaceFile(
                None if mutation.after.sha256 is None else store.blob(mutation.after.sha256),
                mutation.after.mode,
            )
            for mutation in original.plan.mutations
        }
        prepared = prepare_workspace_transaction(anchor, desired, request_id="new-projection")
        projection = store.save(prepared)
        assert projection.transaction_id != original.transaction_id
        assert projection.plan.source.root_identity != original.plan.source.root_identity
        assert projection.plan.mutations == original.plan.mutations
        anchor_lease = leases.acquire(
            projection.plan.source.workspace_id, "projection-test", ttl_seconds=60
        )
        yield values, anchor, projection, anchor_lease
    finally:
        if anchor_lease is not None:
            leases.release(anchor_lease)
        leases.release(original_lease)
        _close(values)


def test_published_projection_on_real_anchor_rejects_old_plan_and_snapshot(tmp_path: Path) -> None:
    """原 publish 真正改写 A 后，干净源和原 Snapshot 两项独立保护均拒绝。"""
    with _linked_anchor(tmp_path) as (values, anchor, projection, lease):
        user, baseline, *_paths, store, git_store, leases, runtime, _original, _user_lease = values
        binding = runtime.bind_repository(anchor, projection.plan.source.workspace_id)
        assert binding.head_oid == baseline
        assert _run(anchor, "status", "--porcelain=v2", "-z") == b""
        assert verify_workspace_snapshot(projection.plan.source, anchor) == projection.plan.source
        published = WorkspaceTransactionRuntime(store, leases).publish(
            projection.transaction_id,
            anchor,
            approval_fingerprint=projection.plan.fingerprint,
            lease=lease,
        )
        assert published.state == "published" and published.plan == projection.plan
        assert store.load(projection.transaction_id) == published
        assert (anchor / "modify.txt").read_bytes() == b"after\n"
        assert not (anchor / "delete.txt").exists()
        assert (anchor / "added.bin").read_bytes() == b"\0\x01\x02"
        with pytest.raises(KernelError) as dirty:
            runtime.plan_worktree(projection.transaction_id, anchor)
        assert dirty.value.code == "delivery_dirty_conflict"
        with pytest.raises(KernelError) as stale:
            verify_workspace_snapshot(projection.plan.source, anchor)
        assert stale.value.code == "execution_plan_stale"
        assert list((git_store.root / "worktrees").iterdir()) == []
        assert _run(user, "status", "--porcelain=v2", "-z") == b""
        _record(
            "published-before-plan",
            anchor=str(anchor),
            source=projection.plan.source.model_dump(mode="json"),
            clean_binding=binding.model_dump(mode="json"),
            transaction=published.model_dump(mode="json"),
            anchor_status_hex=_run(anchor, "status", "--porcelain=v2", "-z").hex(),
            plan_error=dirty.value.code,
            snapshot_error=stale.value.code,
            user_status_hex=_run(user, "status", "--porcelain=v2", "-z").hex(),
            delivery_worktree_created=False,
        )


def test_old_checkpoint_materializes_target_without_publishing_source_transaction(
    tmp_path: Path,
) -> None:
    """原正对照在 D 物化完整目标，原 T 仍 prepared，来源根不被发布。"""
    values = _checkpoint(tmp_path, request="prepared-control")
    user, baseline, *_paths, store, _git_store, leases, _runtime, transaction, lease, ready, cp = (
        values
    )
    try:
        actual = store.load(transaction.transaction_id)
        assert actual == transaction and actual.state == "prepared"
        assert verify_workspace_snapshot(actual.plan.source, user) == actual.plan.source
        assert _run(user, "status", "--porcelain=v2", "-z") == b""
        assert _run(user, "rev-parse", "HEAD").decode().strip() == baseline
        delivery = Path(ready.plan.path)
        assert (delivery / "modify.txt").read_bytes() == b"after\n"
        assert not (delivery / "delete.txt").exists()
        assert (delivery / "added.bin").read_bytes() == b"\0\x01\x02"
        assert _run(delivery, "write-tree").decode().strip() == cp.tree_oid
        _record(
            "prepared-checkpoint-control",
            transaction=actual.model_dump(mode="json"),
            worktree=ready.model_dump(mode="json"),
            checkpoint=cp.model_dump(mode="json"),
            source_status_hex=_run(user, "status", "--porcelain=v2", "-z").hex(),
            delivery_tree_oid=_run(delivery, "write-tree").decode().strip(),
            scope="original-component-ordinary-clean-source",
        )
    finally:
        leases.release(lease)
        _close(values)


def test_publishing_after_worktree_ready_still_rejects_checkpoint(tmp_path: Path) -> None:
    """仅提前创建 D 不能解除冲突：原 Checkpoint 仍要求来源绑定不变。"""
    values = _prepared(tmp_path, request="publish-after-ready")
    user, baseline, *_paths, store, git_store, leases, runtime, transaction, lease = values
    try:
        planned = runtime.plan_worktree(transaction.transaction_id, user)
        ready = runtime.create_worktree(
            planned.worktree_id,
            user,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        delivery = Path(ready.plan.path)
        assert (delivery / "modify.txt").read_bytes() == b"before\n"
        published = WorkspaceTransactionRuntime(store, leases).publish(
            transaction.transaction_id,
            user,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        assert published.state == "published"
        with pytest.raises(KernelError) as error:
            runtime.create_checkpoint(ready.worktree_id, user, lease=lease)
        assert error.value.code == "delivery_dirty_conflict"
        assert git_store.checkpoint_for_worktree(ready.worktree_id) is None
        assert git_store.load_worktree(ready.worktree_id) == ready
        assert (delivery / "modify.txt").read_bytes() == b"before\n"
        assert _run(delivery, "status", "--porcelain=v2", "-z") == b""
        assert _run(user, "rev-parse", "HEAD").decode().strip() == baseline
        _record(
            "published-after-ready",
            transaction=published.model_dump(mode="json"),
            worktree=ready.model_dump(mode="json"),
            checkpoint_error=error.value.code,
            checkpoint_saved=False,
            delivery_status_hex=_run(delivery, "status", "--porcelain=v2", "-z").hex(),
        )
    finally:
        leases.release(lease)
        _close(values)


def test_clean_private_anchor_needs_explicit_source_resolver(tmp_path: Path) -> None:
    """不发布 T 只解决顺序；原 commonDir 邻接推导不能定位新的私有 A。"""
    with _linked_anchor(tmp_path) as (values, anchor, projection, lease):
        user, _baseline, *_paths, store, git_store, _leases, runtime, _original, _user_lease = (
            values
        )
        planned = runtime.plan_worktree(projection.transaction_id, anchor)
        assert store.load(projection.transaction_id).state == "prepared"
        assert (
            planned.plan.repository.root_path_sha256
            != runtime.bind_repository(user, values[-2].plan.source.workspace_id).root_path_sha256
        )
        with pytest.raises(KernelError) as error:
            runtime.create_worktree(
                planned.worktree_id,
                anchor,
                approval_fingerprint=projection.plan.fingerprint,
                lease=lease,
            )
        assert error.value.code == "git_worktree_binding_invalid"
        actual = git_store.load_worktree(planned.worktree_id)
        assert actual.state == "creating" and actual.binding is None
        assert Path(actual.plan.path).is_dir()
        # 原回链观察器封装了内部 RuntimeError；直接只读求证原邻接解析器的拒绝。
        with pytest.raises(KernelError) as resolver:
            runtime._repository_root_from_binding(planned.plan.repository, Path(actual.plan.path))
        assert resolver.value.code == "git_repository_changed"
        assert _run(Path(actual.plan.path), "status", "--porcelain=v2", "-z") == b""
        assert _run(anchor, "status", "--porcelain=v2", "-z") == b""
        assert store.load(projection.transaction_id) == projection
        assert git_store.checkpoint_for_worktree(planned.worktree_id) is None
        _record(
            "private-anchor-resolver",
            transaction=projection.model_dump(mode="json"),
            planned=planned.model_dump(mode="json"),
            actual_worktree=actual.model_dump(mode="json"),
            create_error=error.value.code,
            resolver_error=resolver.value.code,
            anchor_status_hex=_run(anchor, "status", "--porcelain=v2", "-z").hex(),
            native_effect_retained=True,
            scope="original-component-real-detached-private-anchor",
        )
