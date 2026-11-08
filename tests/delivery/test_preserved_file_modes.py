"""Workspace事务保留现有文件的真实权限（ADR 0068第2版）。

缺陷：原合同只用Git能表示的0644/0755记录文件版本，umask 002检出的0664文件、0777文件都无法修改。
修复：只在事务涉及其他普通权限时启用保留权限代际（Plan v3/v4），其余事务仍是原代际、字节不变。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.delivery import filesystem, workspace_record_codec, workspace_store_schema
from harnessix.delivery.contracts import (
    WorkspaceFileVersion,
    WorkspaceMutation,
    WorkspaceTransactionPlan,
)
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.file_modes import observed_permission_mode
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.preserved_mode_contracts import (
    WorkspaceDiffDocumentV2,
    WorkspaceFileVersionV2,
    WorkspaceMutationV2,
    WorkspaceTransactionPlanV3,
    WorkspaceTransactionPlanV4,
    preserves_file_modes,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import (
    WorkspaceActionReviewDocumentV2,
    WorkspacePatchFile,
    WorkspacePatchInput,
    build_workspace_action_review,
    parse_workspace_action_review,
)
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionPlanV2
from harnessix.domain.review_text import review_text_for_protection
from harnessix.patches.contracts import ExactEdit
from harnessix.product_config.git_delivery_source import _require_git_representable_modes
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _approval
from tests.product_config.test_product_patch_rollback import (
    approve,
    product,
    result,
    rollback_step,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX文件权限")
SOURCE = b"value = 1\nother = 2\n"
EDITED = b"value = 3\nother = 2\n"
SHA = hashlib.sha256(SOURCE).hexdigest()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _schema_version(store_root: Path) -> str:
    database = sqlite3.connect(store_root / "transactions.db")
    try:
        (value,) = database.execute(
            "SELECT value FROM delivery_metadata WHERE key='schema_version'"
        ).fetchone()
        return str(value)
    finally:
        database.close()


def _workspace(tmp_path: Path, modes: dict[str, int]) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    for name, mode in modes.items():
        (root / name).write_bytes(SOURCE)
        (root / name).chmod(mode)
    return root


# ---------------------------------------------------------------- 产品链路：补丁、审批、发布、回滚


def _proposal(operation: str) -> WorkspacePatchInput:
    if operation == "edit":
        item = WorkspacePatchFile(
            operation="edit",
            path="src/target.py",
            expected_sha256=SHA,
            edits=(ExactEdit(old_text="value = 1", new_text="value = 3"),),
        )
    elif operation == "replace":
        item = WorkspacePatchFile(
            operation="replace",
            path="src/target.py",
            expected_sha256=SHA,
            content=EDITED.decode(),
            mode=0o644,
        )
    else:
        item = WorkspacePatchFile(operation="delete", path="src/target.py", expected_sha256=SHA)
    return WorkspacePatchInput(files=(item,))


async def _patch(runtime, provider, root: Path, operation: str):
    provider.steps = (_action_step(_proposal(operation)), answer("完成"))
    thread = await runtime.create_thread(str(root))
    waiting = await runtime.run_turn(thread.thread_id, "修改文件", request_id=operation)
    assert waiting.status is TurnStatus.WAITING_APPROVAL
    completed = await approve(runtime, thread.thread_id, waiting)
    assert result(completed).outcome == "succeeded"
    return thread.thread_id, _approval(waiting).plan_id


@pytest.mark.parametrize("mode", [0o664, 0o777, 0o600])
async def test_edit_keeps_the_real_mode_of_umask_002_and_0777_files(tmp_path, mode):
    """umask 002检出的0664、WSL/镜像常见的0777以及0600文件都能修改，权限保持不变。"""
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        target = root / "src/target.py"
        target.write_bytes(SOURCE)
        target.chmod(mode)
        _, plan_id = await _patch(runtime, provider, root, "edit")
        assert target.read_bytes() == EDITED and _mode(target) == mode
        plan = transactions.load(plan_id).plan
        assert preserves_file_modes(plan)
        (mutation,) = plan.mutations
        assert type(mutation) is WorkspaceMutationV2
        assert mutation.before.mode == mutation.after.mode == mode
        diff = build_workspace_diff(plan, transactions)
        assert type(diff) is WorkspaceDiffDocumentV2
        assert f"preserved mode {mode:o}\n" in diff.text
        assert "old mode" not in diff.text and "new mode" not in diff.text


async def test_replace_sets_the_requested_mode_and_shows_the_change(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        target = root / "src/target.py"
        target.write_bytes(SOURCE)
        target.chmod(0o777)
        _, plan_id = await _patch(runtime, provider, root, "replace")
        assert target.read_bytes() == EDITED and _mode(target) == 0o644
        diff = build_workspace_diff(transactions.load(plan_id).plan, transactions)
        assert "old mode 777\nnew mode 644\n" in diff.text


@pytest.mark.parametrize("operation", ["edit", "replace", "delete"])
async def test_rollback_restores_the_original_content_and_mode(tmp_path, operation):
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        target = root / "src/target.py"
        target.write_bytes(SOURCE)
        target.chmod(0o664)
        thread_id, plan_id = await _patch(runtime, provider, root, operation)
        provider.steps = (rollback_step(plan_id), answer("回滚完成"))
        waiting = await runtime.run_turn(thread_id, "回滚", request_id=f"{operation}-rollback")
        completed = await approve(runtime, thread_id, waiting)
        assert result(completed).outcome == "succeeded"
        assert target.read_bytes() == SOURCE and _mode(target) == 0o664
        rollback = transactions.load(_approval(waiting).plan_id).plan
        assert preserves_file_modes(rollback)


async def test_git_representable_files_keep_the_original_generation(tmp_path):
    """全部是0644/0755的事务仍生成原代际：成员类型、Plan版本与账本Schema都不变。"""
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        target = root / "src/target.py"
        target.write_bytes(SOURCE)
        target.chmod(0o644)
        _, plan_id = await _patch(runtime, provider, root, "edit")
        plan = transactions.load(plan_id).plan
        assert type(plan) in (WorkspaceTransactionPlan, WorkspaceTransactionPlanV2)
        assert all(type(item) is WorkspaceMutation for item in plan.mutations)
        assert plan.model_dump(mode="json")["mutations"][0]["after"]["mode"] == 0o644
        assert _schema_version(tmp_path / "state/delivery") in {"2", "3"}


# ------------------------------------------------ 规划：两条Snapshot路线各自按需选择代际


@pytest.mark.parametrize(
    ("modes", "preserved"),
    [
        ({"a.txt": 0o644, "b.txt": 0o755}, False),
        ({"a.txt": 0o664}, True),
        ({"a.txt": 0o644, "b.txt": 0o777}, True),
    ],
)
def test_snapshot_v1_route_uses_plan_v3_only_when_needed(tmp_path, modes, preserved):
    root = _workspace(tmp_path, modes)
    desired = {name: DesiredWorkspaceFile(EDITED, mode) for name, mode in modes.items()}
    plan = prepare_workspace_transaction(root, desired, request_id="v1-route").plan
    assert type(plan) is (WorkspaceTransactionPlanV3 if preserved else WorkspaceTransactionPlan)
    version_type = WorkspaceFileVersionV2 if preserved else WorkspaceFileVersion
    assert all(type(item.before) is version_type for item in plan.mutations)
    assert [item.after.mode for item in plan.mutations] == list(modes.values())


@pytest.mark.parametrize(("mode", "preserved"), [(0o755, False), (0o664, True)])
def test_snapshot_v2_route_uses_plan_v4_only_when_needed(tmp_path, mode, preserved):
    root = _workspace(tmp_path, {"a.txt": mode})
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        plan = prepare_workspace_transaction_v2(
            root,
            {"a.txt": DesiredWorkspaceFile(EDITED, mode)},
            request_id="v2-route",
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store.blob,
        ).plan
    assert type(plan) is (WorkspaceTransactionPlanV4 if preserved else WorkspaceTransactionPlanV2)


def test_special_permission_bits_are_still_rejected(tmp_path):
    assert observed_permission_mode(stat.S_IFREG | 0o664) == 0o664
    for special in (stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX):
        with pytest.raises(KernelError) as rejected:
            observed_permission_mode(stat.S_IFREG | special | 0o755)
        assert rejected.value.code == "delivery_metadata_unsupported"
    root = _workspace(tmp_path, {"a.txt": 0o644})
    (root / "a.txt").chmod(0o4755)
    if not _mode(root / "a.txt") & stat.S_ISUID:
        pytest.skip("文件系统不保留setuid位")
    with pytest.raises(KernelError) as planned:
        prepare_workspace_transaction(
            root, {"a.txt": DesiredWorkspaceFile(EDITED, 0o755)}, request_id="setuid"
        )
    assert planned.value.code == "delivery_metadata_unsupported"


def test_generations_cannot_be_mixed_inside_one_contract(tmp_path):
    v2 = WorkspaceFileVersionV2(presence="file", sha256=SHA, size=len(SOURCE), mode=0o664)
    absent = WorkspaceFileVersionV2(presence="absent", size=0)
    with pytest.raises(ValidationError, match="文件版本代际不一致"):
        WorkspaceMutation(path="a.txt", before=v2, after=absent)
    with pytest.raises(ValidationError):
        WorkspaceFileVersionV2(presence="file", sha256=SHA, size=1, mode=0o1000)
    root = _workspace(tmp_path, {"a.txt": 0o664})
    plan = prepare_workspace_transaction(
        root, {"a.txt": DesiredWorkspaceFile(EDITED, 0o664)}, request_id="mixed"
    ).plan
    # 子类实例能通过原字段的类型检查，代际守卫保证原Plan不夹带新代际成员。
    fields = {name: getattr(plan, name) for name in WorkspaceTransactionPlan.model_fields}
    fields["spec_version"] = "harnessix.workspace-transaction-plan/v1"
    with pytest.raises(ValidationError, match="成员代际不一致"):
        WorkspaceTransactionPlan.model_validate(fields)


# ---------------------------------------------------------------- 发布、崩溃恢复与账本


def _published_runtime(tmp_path: Path, root: Path, desired, *, request_id: str):
    prepared = prepare_workspace_transaction(root, desired, request_id=request_id)
    store = SQLiteWorkspaceTransactionStore(tmp_path / "state/transactions")
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db")
    return prepared, store, leases, WorkspaceTransactionRuntime(store, leases)


def test_crash_after_first_member_recovers_with_preserved_modes(tmp_path, monkeypatch):
    root = _workspace(tmp_path, {"a.txt": 0o664, "b.txt": 0o777})
    desired = {
        "a.txt": DesiredWorkspaceFile(EDITED, 0o664),
        "b.txt": DesiredWorkspaceFile(EDITED, 0o777),
    }
    prepared, store, leases, runtime = _published_runtime(tmp_path, root, desired, request_id="r")
    try:
        record = store.save(prepared)
        lease = leases.acquire(record.plan.source.workspace_id, "test", ttl_seconds=60)

        def crash(point: str) -> None:
            if point == "effect_applied:0":
                raise RuntimeError("crash")

        monkeypatch.setattr(filesystem, "_fault", crash)
        with pytest.raises(RuntimeError):
            runtime.publish(
                record.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
        assert _mode(root / "a.txt") == 0o664 and (root / "a.txt").read_bytes() == EDITED
        interrupted = runtime.reconcile(record.transaction_id, root)
        assert interrupted.state == "interrupted" and interrupted.cursor == 1
        monkeypatch.setattr(filesystem, "_fault", lambda _: None)
        published = runtime.publish(
            record.transaction_id, root, approval_fingerprint=record.plan.fingerprint, lease=lease
        )
    finally:
        leases.close()
        store.close()
    assert published.state == "published"
    assert [_mode(root / name) for name in ("a.txt", "b.txt")] == [0o664, 0o777]
    assert all((root / name).read_bytes() == EDITED for name in ("a.txt", "b.txt"))


_HARD_EXIT = """
import os, sys
from pathlib import Path
from uuid import UUID
from harnessix.delivery import filesystem
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.leases import WorkspaceLeaseStore
store_root, lease_path, workspace, transaction_id = sys.argv[1:]
filesystem._fault = lambda point: os._exit(87) if point == "effect_applied:0" else None
with SQLiteWorkspaceTransactionStore(Path(store_root)) as store:
    with WorkspaceLeaseStore(Path(lease_path)) as leases:
        record = store.load(UUID(transaction_id))
        lease = leases.acquire(record.plan.source.workspace_id, "worker", ttl_seconds=60)
        WorkspaceTransactionRuntime(store, leases).publish(
            record.transaction_id, Path(workspace),
            approval_fingerprint=record.plan.fingerprint, lease=lease)
raise AssertionError("未到达硬退出切点")
"""


def test_real_process_exit_reconciles_preserved_modes_without_repeating_effects(tmp_path):
    root = _workspace(tmp_path, {"a.txt": 0o664, "b.txt": 0o664})
    desired = {name: DesiredWorkspaceFile(EDITED, 0o664) for name in ("a.txt", "b.txt")}
    store_root, lease_path = tmp_path / "state/transactions", tmp_path / "state/leases.db"
    with SQLiteWorkspaceTransactionStore(store_root) as store:
        record = store.save(prepare_workspace_transaction(root, desired, request_id="hard"))
    completed = subprocess.run(
        [sys.executable, "-c", _HARD_EXIT, str(store_root), str(lease_path), str(root)]
        + [str(record.transaction_id)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 87, completed.stderr
    with SQLiteWorkspaceTransactionStore(store_root) as store:
        with WorkspaceLeaseStore(lease_path) as leases:
            runtime = WorkspaceTransactionRuntime(store, leases)
            interrupted = runtime.reconcile(record.transaction_id, root)
            assert interrupted.state == "interrupted" and interrupted.cursor == 1
            lease = leases.acquire(record.plan.source.workspace_id, "worker", ttl_seconds=60)
            published = runtime.publish(
                record.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
    assert published.state == "published"
    assert all(_mode(root / name) == 0o664 for name in ("a.txt", "b.txt"))


def test_store_admits_schema_4_and_old_readers_fail_closed(tmp_path, monkeypatch):
    root = _workspace(tmp_path, {"a.txt": 0o664})
    prepared = prepare_workspace_transaction(
        root, {"a.txt": DesiredWorkspaceFile(EDITED, 0o664)}, request_id="schema"
    )
    store_root = tmp_path / "state/transactions"
    with SQLiteWorkspaceTransactionStore(store_root) as store:
        saved = store.save(prepared)
    assert _schema_version(store_root) == "4"
    with SQLiteWorkspaceTransactionStore(store_root, read_only=True) as store:
        loaded = store.load(saved.transaction_id)
        payload = store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
    assert loaded == saved and type(loaded.plan) is WorkspaceTransactionPlanV3
    assert '"harnessix.workspace-stored-record/v4"' in payload
    # 旧程序：不认识Schema4，整个账本拒绝打开；也不认识stored-record/v4，单条记录判为损坏。
    monkeypatch.setattr(
        workspace_store_schema, "SUPPORTED_SCHEMA_VERSIONS", frozenset({"1", "2", "3"})
    )
    with pytest.raises(KernelError) as opened:
        SQLiteWorkspaceTransactionStore(store_root, read_only=True)
    assert opened.value.code == "delivery_store_version"
    monkeypatch.undo()
    old_envelopes = {
        key: value
        for key, value in workspace_record_codec._ENVELOPE_TYPES.items()
        if not key.endswith("/v4")
    }
    monkeypatch.setattr(workspace_record_codec, "_ENVELOPE_TYPES", old_envelopes)
    with SQLiteWorkspaceTransactionStore(store_root, read_only=True) as store:
        with pytest.raises(KernelError) as decoded:
            store.load(saved.transaction_id)
    assert decoded.value.code == "delivery_store_corrupt"


def test_original_generation_store_stays_below_schema_4(tmp_path):
    root = _workspace(tmp_path, {"a.txt": 0o644})
    prepared = prepare_workspace_transaction(
        root, {"a.txt": DesiredWorkspaceFile(EDITED, 0o644)}, request_id="old"
    )
    store_root = tmp_path / "state/transactions"
    with SQLiteWorkspaceTransactionStore(store_root) as store:
        store.save(prepared)
    assert _schema_version(store_root) == "2"


# ---------------------------------------------------------------- 审阅与Git交付边界


def test_review_document_v2_round_trips_and_shows_preserved_mode(tmp_path):
    root = _workspace(tmp_path, {"a.txt": 0o664})
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(
            prepare_workspace_transaction(
                root, {"a.txt": DesiredWorkspaceFile(EDITED, 0o664)}, request_id="review"
            )
        )
        diff = build_workspace_diff(record.plan, store)
    document = build_workspace_action_review(record.plan, diff.entries, diff.text)
    assert type(document) is WorkspaceActionReviewDocumentV2
    assert document.summary.spec_version == "harnessix.workspace-action-review/v2"
    assert document.entries[0].entry.before.mode == 0o664
    parsed = parse_workspace_action_review(document.to_jsonl())
    assert parsed == document and type(parsed) is WorkspaceActionReviewDocumentV2
    # 公开保护按已知Review重建完整正文再扫描；v2不能退化成只逐行扫描的未知格式。
    assert review_text_for_protection(document.to_jsonl(), checkpoint=lambda: None) == diff.text


def test_git_delivery_rejects_preserved_mode_patches_with_a_clear_reason(tmp_path):
    root = _workspace(tmp_path, {"a.txt": 0o664, "b.txt": 0o644})
    preserved = prepare_workspace_transaction(
        root, {"a.txt": DesiredWorkspaceFile(EDITED, 0o664)}, request_id="git-new"
    ).plan
    original = prepare_workspace_transaction(
        root, {"b.txt": DesiredWorkspaceFile(EDITED, 0o644)}, request_id="git-old"
    ).plan
    _require_git_representable_modes(SimpleNamespace(record=SimpleNamespace(plan=original)))
    with pytest.raises(KernelError) as rejected:
        _require_git_representable_modes(SimpleNamespace(record=SimpleNamespace(plan=preserved)))
    assert rejected.value.code == "git_delivery_source_mode_unsupported"
    assert "0644/0755" in rejected.value.message
