"""独立Git领域复核完整父历史，真实A/T/D联动不发布来源事务。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_workspace_snapshot as snapshot_dispatch
from harnessix.delivery.git import GitDeliveryRuntime
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_v2_contracts import (
    WorkspaceTransactionPlanV2,
    WorkspaceTransactionRecordV2,
)
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot_v2 import verify_workspace_snapshot_v2
from scripts.windows_git_native_branch_observation import contract
from tests.delivery.test_git import _COMMIT_TIME, _git, _run
from tests.governance.test_windows_git_trace2_input_binding import _metadata_changes

pytestmark = pytest.mark.skipif(os.name != "posix", reason="本组验证真实POSIX私有来源")


def _checkpoint() -> None:
    pass


def _evidence(name: str, **facts: object) -> None:
    target = os.environ.get("HARNESSIX_GIT_PARENT_EVIDENCE")
    if target is None:
        return
    path = Path(target) / f"{name}.json"
    with path.open("x", encoding="utf-8") as output:
        json.dump(facts, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
    path.chmod(0o600)


@dataclass(frozen=True)
class Source:
    user: Path
    anchor: Path
    baseline: str
    desired: dict[str, DesiredWorkspaceFile]
    store: SQLiteWorkspaceTransactionStore
    git_store: SQLiteGitDeliveryStore
    leases: WorkspaceLeaseStore
    runtime: GitDeliveryRuntime
    transaction: WorkspaceTransactionRecordV2


@contextmanager
def _source(
    tmp_path: Path, count: int = 1, *, depth: int = 2, object_format: str = "sha1"
) -> Iterator[Source]:
    user = tmp_path / "user"
    user.mkdir()
    _run(user, "init", "-q", "--object-format=" + object_format)
    _run(user, "config", "user.name", "Harnessix Test")
    _run(user, "config", "user.email", "test@harnessix.invalid")
    _run(user, "config", "core.autocrlf", "false")
    desired = {}
    for index in range(count):
        parents = [f"d{index:03}", *(f"p{level:02}" for level in range(1, depth))]
        path = "/".join([*parents, "leaf.txt"])
        member = user / path
        member.parent.mkdir(parents=True)
        member.write_bytes(b"before\n")
        member.chmod(0o644)
        desired[path] = DesiredWorkspaceFile(b"after\n", 0o644)
    _run(user, "add", "--all")
    _run(user, "commit", "-qm", "baseline")
    baseline = _run(user, "rev-parse", "HEAD").decode().strip()
    anchor = tmp_path / "private/anchors/a"
    anchor.parent.mkdir(parents=True, mode=0o700)
    anchor.parent.parent.chmod(0o700)
    _run(user, "worktree", "add", "--detach", str(anchor), baseline)
    anchor.chmod(0o700)
    with (
        SQLiteWorkspaceTransactionStore(tmp_path / "state/workspace") as store,
        SQLiteGitDeliveryStore(tmp_path / "state/git") as git_store,
        WorkspaceLeaseStore(tmp_path / "state/leases.db") as leases,
    ):
        prepared = prepare_workspace_transaction_v2(
            anchor,
            desired,
            request_id="complete-git-parent-history",
            checkpoint=_checkpoint,
            write_blob=store.put_blob,
            read_blob=store.blob,
        )
        transaction = store.save(prepared)
        assert type(prepared.plan) is WorkspaceTransactionPlanV2
        assert type(transaction) is WorkspaceTransactionRecordV2
        runtime = GitDeliveryRuntime(
            store, git_store, leases, _git(), source_resolver=lambda _: anchor
        )
        yield Source(
            user, anchor, baseline, desired, store, git_store, leases, runtime, transaction
        )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize(("count", "depth"), [(1, 2), (128, 2), (255, 2), (1, 32)])
def test_real_snapshot2_prepared_anchor_materializes_only_delivery(
    tmp_path: Path, count: int, depth: int, object_format: str
) -> None:
    with _source(tmp_path, count, depth=depth, object_format=object_format) as source:
        transaction, runtime = source.transaction, source.runtime
        snapshot = transaction.plan.source
        history = read_workspace_parent_closure(snapshot, source.store.blob, checkpoint=_checkpoint)
        assert len(snapshot.resources) == count + 1
        assert len(history) == count * depth + 1
        assert {item.path for item in history} == {
            ".",
            *(
                "/".join(path.split("/")[:i])
                for path in source.desired
                for i in range(1, depth + 1)
            ),
        }
        assert _run(source.anchor, "rev-parse", "--abbrev-ref", "HEAD") == b"HEAD\n"
        planned = runtime.plan_worktree(transaction.transaction_id, source.anchor)
        lease = source.leases.acquire(snapshot.workspace_id, "git-parent-test", ttl_seconds=300)
        ready = runtime.create_worktree(
            planned.worktree_id,
            source.anchor,
            approval_fingerprint=transaction.plan.fingerprint,
            lease=lease,
        )
        delivery = Path(ready.plan.path)
        assert all((delivery / path).read_bytes() == b"before\n" for path in source.desired)
        cp = runtime.create_checkpoint(ready.worktree_id, source.anchor, lease=lease)
        assert cp.base_commit_oid == source.baseline
        assert cp.tree_oid != cp.base_tree_oid
        assert _run(delivery, "write-tree").decode().strip() == cp.tree_oid
        assert all((delivery / path).read_bytes() == b"after\n" for path in source.desired)
        assert runtime.create_checkpoint(ready.worktree_id, source.anchor, lease=lease) == cp
        assert source.store.load(transaction.transaction_id) == transaction
        assert transaction.state == "prepared" and transaction.sequence == transaction.cursor == 0
        assert (
            verify_workspace_snapshot_v2(
                snapshot, source.anchor, checkpoint=_checkpoint, read_blob=source.store.blob
            )
            == snapshot
        )
        for root in (source.anchor, source.user):
            assert all((root / path).read_bytes() == b"before\n" for path in source.desired)
            assert _run(root, "status", "--porcelain=v2", "--untracked-files=all", "-z") == b""
            assert _run(root, "rev-parse", "HEAD").decode().strip() == source.baseline
        commit = runtime.plan_commit(
            cp.checkpoint_id,
            source.anchor,
            branch="harnessix/complete-parent-test",
            author_name="Harnessix Test",
            author_email="test@harnessix.invalid",
            message="Apply independently approved checkpoint",
            authored_at=_COMMIT_TIME,
        )
        with pytest.raises(KernelError) as rejected:
            runtime.commit(
                commit.commit_id,
                source.anchor,
                approval_fingerprint=transaction.plan.fingerprint,
                lease=lease,
            )
        assert rejected.value.code == "git_commit_approval_mismatch"
        assert source.git_store.load_commit(commit.commit_id) == commit
        assert runtime._ref(source.anchor, commit.spec.branch_ref) is None
        committed = runtime.commit(
            commit.commit_id,
            source.anchor,
            approval_fingerprint=commit.spec.fingerprint,
            lease=lease,
        )
        assert (
            committed.state == "committed"
            and committed.commit_oid == commit.spec.expected_commit_oid
        )
        assert runtime._ref(source.anchor, commit.spec.branch_ref) == committed.commit_oid
        assert source.store.load(transaction.transaction_id) == transaction
        assert (
            verify_workspace_snapshot_v2(
                snapshot, source.anchor, checkpoint=_checkpoint, read_blob=source.store.blob
            )
            == snapshot
        )
        for root in (source.anchor, source.user):
            assert _run(root, "status", "--porcelain=v2", "--untracked-files=all", "-z") == b""
            assert _run(root, "rev-parse", "HEAD").decode().strip() == source.baseline
        _evidence(
            f"prepared-{count}-depth{depth}-{object_format}",
            snapshot=snapshot.model_dump(mode="json"),
            complete_parent_count=len(history),
            transaction=transaction.model_dump(mode="json"),
            worktree=ready.model_dump(mode="json"),
            checkpoint=cp.model_dump(mode="json"),
            commit_plan=commit.model_dump(mode="json"),
            commit=committed.model_dump(mode="json"),
            old_approval_error=rejected.value.code,
            source_and_user_clean=True,
        )


@pytest.mark.parametrize("count", [128, 255])
def test_original_v1_capacity_failure_remains(tmp_path: Path, count: int) -> None:
    with _source(tmp_path, count) as source:
        with pytest.raises(KernelError) as rejected:
            prepare_workspace_transaction(source.anchor, source.desired, request_id="legacy")
        assert rejected.value.code == "workspace_snapshot_limit"
        assert source.git_store._db.execute("SELECT count(*) FROM git_worktrees").fetchone() == (0,)


def _assert_no_worktree(source: Source) -> None:
    for table in ("git_worktrees", "git_worktree_events", "git_checkpoints", "git_commits"):
        assert source.git_store._db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert list((source.git_store.root / "worktrees").iterdir()) == []


@pytest.mark.parametrize("stage", ["before_load", "after_binding"])
@pytest.mark.parametrize("layer", ["manifest", "chunk"])
@pytest.mark.parametrize("damage", ["missing", "wrong_bytes"])
def test_missing_or_wrong_history_refused_without_record_or_repair(
    tmp_path: Path, stage: str, layer: str, damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _source(tmp_path) as source:
        snapshot = source.transaction.plan.source
        manifest = json.loads(source.store.blob(snapshot.parent_closure.sha256))
        digest = (
            snapshot.parent_closure.sha256
            if layer == "manifest"
            else manifest["chunks"][-1]["sha256"]
        )
        path = source.store._blobs / digest
        body = path.read_bytes()
        before = source.store._db.execute("SELECT payload FROM workspace_transactions").fetchall()

        def corrupt() -> None:
            if damage == "missing":
                path.unlink()
            else:
                path.write_bytes(body[:-1] + b"!")

        if stage == "before_load":
            corrupt()
        else:
            original = source.runtime.bind_repository

            def bind(*args, **kwargs):
                binding = original(*args, **kwargs)
                corrupt()
                return binding

            monkeypatch.setattr(source.runtime, "bind_repository", bind)
        with pytest.raises(KernelError) as rejected:
            source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        assert rejected.value.code == (
            "delivery_store_corrupt" if stage == "before_load" else "workspace_closure_corrupt"
        )
        _assert_no_worktree(source)
        assert source.store._db.execute(
            "SELECT payload FROM workspace_transactions"
        ).fetchall() == (before)
        if damage == "missing":
            assert not path.exists()
        else:
            assert path.read_bytes() == body[:-1] + b"!"
        _evidence(
            f"history-{stage}-{layer}-{damage}",
            error_code=rejected.value.code,
            no_worktree_record=True,
            no_history_repair=True,
        )


@pytest.mark.parametrize("change", ["membership", "mode", "identity"])
def test_clean_git_does_not_hide_changed_distant_historical_parent(
    tmp_path: Path, change: str
) -> None:
    with _source(tmp_path, 128) as source:
        parent = source.anchor / "d127/p01"
        leaf = parent / "leaf.txt"
        original_inode = leaf.stat().st_ino
        if change == "membership":
            (parent / "empty-child").mkdir()
        elif change == "mode":
            parent.chmod(0o700)
        else:
            moved = source.anchor.parent / "moved-parent"
            parent.rename(moved)
            parent.mkdir()
            (moved / "leaf.txt").rename(leaf)
        assert leaf.stat().st_ino == original_inode and leaf.read_bytes() == b"before\n"
        assert _run(source.anchor, "status", "--porcelain=v2", "--untracked-files=all", "-z") == b""
        with pytest.raises(KernelError) as rejected:
            source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        assert rejected.value.code == "execution_plan_stale"
        _assert_no_worktree(source)
        assert source.store.load(source.transaction.transaction_id) == source.transaction
        _evidence(
            f"native-parent-{change}", error_code=rejected.value.code, no_worktree_record=True
        )


def test_plan_reads_complete_original_cas_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _source(tmp_path, 255) as source:
        snapshot = source.transaction.plan.source
        manifest = json.loads(source.store.blob(snapshot.parent_closure.sha256))
        original = source.store.blob
        reads = []

        def read(digest: str) -> bytes:
            reads.append(digest)
            return original(digest)

        monkeypatch.setattr(source.store, "blob", read)
        monkeypatch.setattr(source.store, "put_blob", lambda *args: pytest.fail("复核不得写入CAS"))
        source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        assert reads == [
            snapshot.parent_closure.sha256,
            *(row["sha256"] for row in manifest["chunks"]),
        ]


@pytest.mark.parametrize("control", ["cancelled", "timeout", "kernel"])
def test_generation_verification_keeps_original_control_exception_identity(
    tmp_path: Path, control: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _source(tmp_path) as source:
        error = {
            "cancelled": TurnCancelled(),
            "timeout": ReadToolError("timeout"),
            "kernel": KernelError("verification_stopped", "来源复核已停止"),
        }[control]
        operation = snapshot_dispatch.ReadOperation()

        def stop() -> None:
            raise error

        monkeypatch.setattr(operation, "checkpoint", stop)
        monkeypatch.setattr(snapshot_dispatch, "ReadOperation", lambda: operation)
        with pytest.raises(type(error)) as rejected:
            source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        assert rejected.value is error
        _assert_no_worktree(source)
        assert source.store.load(source.transaction.transaction_id) == source.transaction


def test_v1_uses_original_verifier_and_keeps_plan_record_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _source(tmp_path) as source:
        legacy = source.store.save(
            prepare_workspace_transaction(source.anchor, source.desired, request_id="legacy-small")
        )
        original = snapshot_dispatch.verify_workspace_snapshot
        calls = []

        def verify(expected, root):
            calls.append(expected)
            return original(expected, root)

        monkeypatch.setattr(snapshot_dispatch, "verify_workspace_snapshot", verify)
        monkeypatch.setattr(
            snapshot_dispatch,
            "verify_workspace_snapshot_v2",
            lambda *args, **kwargs: pytest.fail("v1不得升级"),
        )
        before = legacy.model_dump_json()
        payload = source.store._db.execute(
            "SELECT payload FROM workspace_transactions WHERE transaction_id=?",
            (str(legacy.transaction_id),),
        ).fetchone()
        source.runtime.plan_worktree(legacy.transaction_id, source.anchor)
        assert calls == [legacy.plan.source]
        assert source.store.load(legacy.transaction_id).model_dump_json() == before
        assert (
            source.store._db.execute(
                "SELECT payload FROM workspace_transactions WHERE transaction_id=?",
                (str(legacy.transaction_id),),
            ).fetchone()
            == payload
        )
        (source.anchor / next(iter(source.desired))).write_bytes(b"dirty\n")
        with pytest.raises(KernelError) as rejected:
            source.runtime.plan_worktree(legacy.transaction_id, source.anchor)
        assert rejected.value.code == "delivery_dirty_conflict" and len(calls) == 1


@pytest.mark.parametrize("guard", ["approval", "root", "lease", "expired_lease"])
def test_snapshot2_preserves_original_create_guards(
    tmp_path: Path, guard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _source(tmp_path) as source:
        planned = source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        lease = source.leases.acquire(
            source.transaction.plan.source.workspace_id, "git-parent-test", ttl_seconds=300
        )
        if guard == "lease":
            lease = source.leases.acquire("f" * 64, "foreign", ttl_seconds=300)
        elif guard == "expired_lease":
            clock = source.leases._clock
            monkeypatch.setattr(source.leases, "_clock", lambda: clock() + 301)
        with pytest.raises(KernelError) as rejected:
            source.runtime.create_worktree(
                planned.worktree_id,
                source.user if guard == "root" else source.anchor,
                approval_fingerprint="0" * 64
                if guard == "approval"
                else source.transaction.plan.fingerprint,
                lease=lease,
            )
        assert (
            rejected.value.code
            == {
                "approval": "git_worktree_approval_mismatch",
                "root": "git_repository_changed",
                "lease": "workspace_lease_lost",
                "expired_lease": "workspace_lease_lost",
            }[guard]
        )
        assert source.git_store.load_worktree(planned.worktree_id) == planned
        assert not Path(planned.plan.path).exists()


@pytest.mark.parametrize("guard", ["root_identity", "head", "common_directory", "backlink"])
def test_snapshot2_preserves_source_binding_guards_before_materialization(
    tmp_path: Path, guard: str
) -> None:
    with _source(tmp_path) as source:
        runtime = source.runtime
        planned = runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        lease = source.leases.acquire(
            source.transaction.plan.source.workspace_id, "git-parent-test", ttl_seconds=300
        )
        ready = runtime.create_worktree(
            planned.worktree_id,
            source.anchor,
            approval_fingerprint=source.transaction.plan.fingerprint,
            lease=lease,
        )
        delivery = Path(ready.plan.path)
        index_before = _run(delivery, "write-tree")
        raw = _run(source.anchor, "rev-parse", "--git-dir").decode().strip()
        admin = Path(raw).resolve(strict=True)
        if guard == "root_identity":
            moved = source.anchor.with_name("old-anchor")
            source.anchor.rename(moved)
            shutil.copytree(moved, source.anchor)
        elif guard == "head":
            tree = _run(source.anchor, "rev-parse", "HEAD^{tree}").decode().strip()
            oid = _run(
                source.anchor, "commit-tree", tree, "-p", source.baseline, input_data=b"new\n"
            )
            _run(source.anchor, "update-ref", "HEAD", oid.decode().strip(), source.baseline)
        elif guard == "common_directory":
            foreign = source.anchor.parent / "foreign.git"
            shutil.copytree(source.user / ".git", foreign)
            (admin / "commondir").write_text(str(foreign) + "\n", encoding="utf-8")
        else:
            (admin / "gitdir").write_text(str(delivery / ".git") + "\n", encoding="utf-8")
        with pytest.raises(KernelError) as rejected:
            runtime.create_checkpoint(ready.worktree_id, source.anchor, lease=lease)
        assert rejected.value.code == "git_worktree_binding_invalid"
        assert source.git_store.checkpoint_for_worktree(ready.worktree_id) is None
        assert _run(delivery, "write-tree") == index_before
        assert (delivery / next(iter(source.desired))).read_bytes() == b"before\n"
        assert source.store.load(source.transaction.transaction_id) == source.transaction


def test_snapshot_helper_bytes_remain_in_original_implementation_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _source(tmp_path) as source:
        planned = source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        lease = source.leases.acquire(
            source.transaction.plan.source.workspace_id, "git-parent-test", ttl_seconds=300
        )
        original = Path.read_bytes

        def changed(path: Path) -> bytes:
            body = original(path)
            return body + b"\n" if path.name == "git_workspace_snapshot.py" else body

        monkeypatch.setattr(Path, "read_bytes", changed)
        with pytest.raises(KernelError) as rejected:
            source.runtime.create_worktree(
                planned.worktree_id,
                source.anchor,
                approval_fingerprint=source.transaction.plan.fingerprint,
                lease=lease,
            )
        assert rejected.value.code == "git_repository_changed"
        assert source.git_store.load_worktree(planned.worktree_id) == planned
        assert not Path(planned.plan.path).exists()


def _baseline(path: str) -> bytes:
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [str(_git()), "show", f"d7e8668:{path}"], cwd=root, check=True, capture_output=True
    ).stdout


def test_native18_delta_is_only_git_four_identity_leaves_and_exact_parser_sha() -> None:
    root = Path(__file__).resolve().parents[2]
    metadata = "scripts/windows_git_native_branch_observation/contract.json"
    parser = "scripts/windows_git_native_branch_observation/contract.py"
    old_body = _baseline(metadata)
    body = (root / metadata).read_bytes()
    original, current = json.loads(old_body), contract.read_contract()
    index = next(
        i
        for i, row in enumerate(original["source_inputs"])
        if row["path"] == "src/harnessix/delivery/git.py"
    )
    assert _metadata_changes(original, current) == {
        f"/source_inputs/{index}/{key}" for key in ("bytes", "sha256", "crlf_bytes", "crlf_sha256")
    }
    assert len(current["source_inputs"]) == 18
    for i, row in enumerate(current["source_inputs"]):
        if i != index:
            assert row == original["source_inputs"][i]
            assert (root / row["path"]).read_bytes() == _baseline(row["path"])
    git_body = (root / current["source_inputs"][index]["path"]).read_bytes()
    crlf = git_body.replace(b"\n", b"\r\n")
    assert current["source_inputs"][index] == {
        "path": "src/harnessix/delivery/git.py",
        "bytes": len(git_body),
        "sha256": hashlib.sha256(git_body).hexdigest(),
        "crlf_bytes": len(crlf),
        "crlf_sha256": hashlib.sha256(crlf).hexdigest(),
    }
    old_sha = hashlib.sha256(old_body).hexdigest().encode()
    new_sha = hashlib.sha256(body).hexdigest().encode()
    old_parser = _baseline(parser)
    assert old_parser.count(old_sha) == 1 and old_sha != new_sha
    assert (root / parser).read_bytes() == old_parser.replace(old_sha, new_sha)
    assert len(contract.source_checks(root, current)) == 18
    with pytest.raises(ValueError, match="^current_source_drift$"):
        contract.source_checks(root, original)
    _evidence(
        "native18-exact-delta",
        changed_leaves=sorted(_metadata_changes(original, current)),
        unchanged_inputs=17,
        contract_sha256=new_sha.decode(),
    )
