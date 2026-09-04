import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision
from harnessix.patches import batch_ledger, ledger, managed_batches
from harnessix.patches.batch_approval_contracts import ManagedPatchBatchPlan
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batches import prepare_patch_batch
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace, digest
from tests.patches.test_managed import APPROVE, REJECT, failure

PATHS = ("one.py", "nested/two.py", "nested/three.py")


def prepare(copy, paths=PATHS, new="after"):
    return prepare_patch_batch(
        copy.workspace,
        PatchBatchProposal(
            files=tuple(
                PatchProposal(
                    path=path,
                    expected_revision=read_file(
                        copy.workspace, ReadFileInput(path=path), ReadOperation()
                    ).revision,
                    edits=(ExactEdit(old_text="before", new_text=new),),
                )
                for path in paths
            )
        ),
        ReadOperation(),
    )


@pytest.fixture
def group_case(tmp_path):
    root = tmp_path / "source"
    for path in PATHS:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"before\r\n")
        target.chmod(0o640)
    with ExitStack() as stack:
        source = stack.enter_context(Workspace(root))
        factory = PatchWorkspaces(tmp_path / "private")
        copy = stack.enter_context(factory.create(source, PATHS, ReadOperation()))
        yield source, factory, copy, ManagedPatchBatches(copy), prepare(copy)


def snapshot(root):
    return tuple(
        (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in (root / name for name in PATHS)
    )


def counts(copy):
    return tuple(
        copy._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("plans", "events", "batches", "batch_approvals")
    )


@pytest.mark.parametrize("decision", [APPROVE, REJECT])
def test_group_save_reply_reopen_without_file_writes(group_case, decision):
    source, factory, copy, groups, batch = group_case
    original, private = snapshot(source.root), snapshot(copy.workspace.root)
    assert groups.lookup("stable", ReadOperation()) is None
    pending = groups.save(batch, "stable", ReadOperation())
    assert pending.decision is None
    assert counts(copy) == (3, 3, 1, 0)
    assert groups.save(batch, "stable", ReadOperation()) == pending
    assert groups.verify(pending.plan.batch_id, ReadOperation()) == pending
    assert pending.plan.manifest == batch.manifest
    result = groups.reply(
        pending.plan.batch_id, pending.plan.approval_fingerprint, decision, ReadOperation()
    )
    assert result.decision == decision and counts(copy) == (3, 3, 1, 1)
    assert all(copy.get(m.plan_id).state == "pending" for m in result.plan.members)
    copy.close()
    with factory.open(copy.workspace_id) as reopened:
        groups = ManagedPatchBatches(reopened)
        assert groups.get(result.plan.batch_id, ReadOperation()) == result
        assert groups.lookup("stable", ReadOperation()) == result
        assert groups.save(batch, "stable", ReadOperation()) == result
        assert (
            groups.reply(
                result.plan.batch_id, result.plan.approval_fingerprint, decision, ReadOperation()
            )
            == result
        )
        assert snapshot(reopened.workspace.root) == private
    assert snapshot(source.root) == original


@pytest.mark.parametrize("decision", [None, APPROVE, REJECT])
@pytest.mark.parametrize("position", range(3))
def test_single_file_entry_cannot_consume_group(group_case, decision, position):
    _, _, copy, groups, batch = group_case
    approval = groups.save(batch, "stable", ReadOperation())
    if decision is not None:
        groups.reply(
            approval.plan.batch_id, approval.plan.approval_fingerprint, decision, ReadOperation()
        )
    member = approval.plan.members[position]
    for action in (
        lambda: copy.reply(member.plan_id, member.approval_fingerprint, APPROVE),
        lambda: copy.execute(member.plan_id, member.approval_fingerprint, ReadOperation()),
        lambda: copy.save(batch.patches[position], member.request_id, ReadOperation()),
    ):
        with failure("patch_batch_member_requires_group"):
            action()
    assert copy.verify(member.plan_id, ReadOperation()).state == "pending"
    assert copy.lookup(member.request_id, ReadOperation()).state == "pending"
    assert counts(copy)[:3] == (3, 3, 1)


@pytest.mark.parametrize("change", ["text", "order"])
def test_same_request_different_plan_conflicts(group_case, change):
    _, _, copy, groups, batch = group_case
    approval = groups.save(batch, "stable", ReadOperation())
    other = prepare(copy, new="different") if change == "text" else prepare(copy, PATHS[::-1])
    with failure("patch_request_conflict"):
        groups.save(other, "stable", ReadOperation())
    another = groups.save(other, "another", ReadOperation())
    assert another.plan.approval_fingerprint != approval.plan.approval_fingerprint
    assert another.plan.batch_id != approval.plan.batch_id
    assert counts(copy) == (6, 6, 2, 0)


def test_approval_binding_and_decision_conflicts(group_case):
    _, _, copy, groups, batch = group_case
    one = groups.save(batch, "one", ReadOperation()).plan
    two = groups.save(batch, "two", ReadOperation()).plan
    for wrong in (two.approval_fingerprint, one.members[0].approval_fingerprint, "0" * 64):
        with failure("patch_approval_mismatch"):
            groups.reply(one.batch_id, wrong, APPROVE, ReadOperation())
    groups.reply(one.batch_id, one.approval_fingerprint, APPROVE, ReadOperation())
    for decision in (REJECT, APPROVE.model_copy(update={"actor": "其他人"})):
        with failure("patch_approval_conflict"):
            groups.reply(one.batch_id, one.approval_fingerprint, decision, ReadOperation())
    assert groups.get(two.batch_id, ReadOperation()).decision is None
    assert counts(copy) == (6, 6, 2, 1)


@pytest.mark.parametrize("position", range(3))
def test_source_drift_prevents_new_reservation_but_not_idempotent_lookup(group_case, position):
    _, _, copy, groups, batch = group_case
    approval = groups.save(batch, "stable", ReadOperation())
    (copy.workspace.root / PATHS[position]).write_text("drift")
    assert groups.save(batch, "stable", ReadOperation()) == approval
    assert groups.get(approval.plan.batch_id, ReadOperation()) == approval
    with pytest.raises(KernelError):
        groups.save(batch, "another", ReadOperation())
    with pytest.raises(KernelError):
        groups.verify(approval.plan.batch_id, ReadOperation())
    assert counts(copy) == (3, 3, 1, 0)


@pytest.mark.parametrize("kind", ["count", "bytes"])
@pytest.mark.parametrize("first", ["single", "batch"])
def test_shared_plan_capacity_is_atomic(group_case, monkeypatch, kind, first):
    _, _, copy, groups, batch = group_case
    size = sum(len(p.before) + len(p.after) for p in batch.patches)
    if kind == "count":
        monkeypatch.setattr(ledger, "MAX_COPY_PLANS", 3)
    else:
        monkeypatch.setattr(ledger, "MAX_PLAN_BYTES", size)
    if first == "single":
        copy.save(batch.patches[0], "single", ReadOperation())
        before = counts(copy)
        with failure("patch_limit_exceeded"):
            groups.save(batch, "group", ReadOperation())
    else:
        groups.save(batch, "group", ReadOperation())
        before = counts(copy)
        with failure("patch_limit_exceeded"):
            copy.save(batch.patches[0], "single", ReadOperation())
    assert counts(copy) == before


def test_metadata_capacity_reserves_final_decision(group_case, monkeypatch):
    _, _, copy, groups, batch = group_case
    # 相同长度请求及 UUID；审批空间按固定上限预留，不依赖未来 actor/reason 内容。
    plan, _ = batch_ledger.plan(copy.workspace_id, "one", batch)
    size = len(plan.model_dump_json().encode()) + batch_ledger.MAX_BATCH_DECISION_BYTES
    monkeypatch.setattr(batch_ledger, "MAX_BATCH_METADATA_BYTES", size - 1)
    with failure("patch_batch_metadata_limit_exceeded"):
        groups.save(batch, "one", ReadOperation())
    assert counts(copy) == (0, 0, 0, 0)
    monkeypatch.setattr(batch_ledger, "MAX_BATCH_METADATA_BYTES", size)
    approval = groups.save(batch, "one", ReadOperation())
    with failure("patch_batch_metadata_limit_exceeded"):
        groups.save(batch, "two", ReadOperation())
    decision = ApprovalDecision(outcome=APPROVE.outcome, actor="\x01" * 256, reason="\x01" * 2000)
    result = groups.reply(
        approval.plan.batch_id, approval.plan.approval_fingerprint, decision, ReadOperation()
    )
    assert result.decision == decision and counts(copy) == (3, 3, 1, 1)


@pytest.mark.parametrize(
    "cut",
    ["batch_reserved", *(f"member_reserved:{i}" for i in range(3)), "reservation_before_commit"],
)
@pytest.mark.parametrize("error", ["storage", "cancel", "timeout"])
def test_mid_transaction_failure_rolls_back_all_members(group_case, monkeypatch, cut, error):
    _, _, copy, groups, batch = group_case
    operation = ReadOperation()

    def fault(point):
        if point == cut:
            if error == "storage":
                raise sqlite3.OperationalError("注入写事务失败")
            if error == "cancel":
                operation.stopped.set()
            else:
                operation.deadline = 0
            operation.checkpoint()

    monkeypatch.setattr(managed_batches, "_fault", fault)
    with pytest.raises(TurnCancelled if error == "cancel" else KernelError):
        groups.save(batch, "stable", operation)
    assert counts(copy) == (0, 0, 0, 0)
    assert not copy._db.in_transaction


@pytest.mark.parametrize("method", ["save", "lookup", "get", "verify", "reply"])
@pytest.mark.parametrize("kind", ["cancel", "timeout"])
def test_all_group_operations_share_deadline_and_cancel(group_case, method, kind):
    _, _, copy, groups, batch = group_case
    approval = groups.save(batch, "stable", ReadOperation()).plan
    operation = ReadOperation()
    if kind == "cancel":
        operation.stopped.set()
    else:
        operation.deadline = 0
    actions = {
        "save": lambda: groups.save(batch, "another", operation),
        "lookup": lambda: groups.lookup("absent", operation),
        "get": lambda: groups.get(approval.batch_id, operation),
        "verify": lambda: groups.verify(approval.batch_id, operation),
        "reply": lambda: groups.reply(
            approval.batch_id, approval.approval_fingerprint, APPROVE, operation
        ),
    }
    with pytest.raises(TurnCancelled if kind == "cancel" else KernelError):
        actions[method]()
    assert counts(copy) == (3, 3, 1, 0)


@pytest.mark.parametrize("request_id", [None, 1, "", "x" * 129])
def test_invalid_stable_request(group_case, request_id):
    _, _, copy, groups, batch = group_case
    for action in (
        lambda: groups.save(batch, request_id, ReadOperation()),
        lambda: groups.lookup(request_id, ReadOperation()),
    ):
        with failure("patch_invalid_request"):
            action()
    assert counts(copy) == (0, 0, 0, 0)


def test_closed_copy_missing_group_and_invalid_id(group_case):
    _, _, copy, groups, _ = group_case
    with failure("patch_invalid_batch"):
        groups.get("not-a-uuid", ReadOperation())
    with failure("patch_batch_not_found"):
        groups.get(uuid4(), ReadOperation())
    copy.close()
    with failure("patch_workspace_closed"):
        groups.lookup("stable", ReadOperation())


def test_unregistered_path_and_corrupt_private_payload(group_case):
    _, _, copy, groups, batch = group_case
    (copy.workspace.root / "outside.py").write_text("before")
    unregistered = prepare(copy, ("outside.py",))
    with failure("patch_path_denied"):
        groups.save(unregistered, "outside", ReadOperation())
    for broken in (replace(batch, patches=batch.patches[::-1]), replace(batch, patches=())):
        with pytest.raises(KernelError):
            groups.save(broken, "stable", ReadOperation())
    assert counts(copy) == (0, 0, 0, 0)


@pytest.mark.parametrize("field", ["batch_id", "workspace_id", "request_id", "members", "extra"])
def test_frozen_group_binding_rejects_modified_input(group_case, field):
    _, _, _, groups, batch = group_case
    plan = groups.save(batch, "stable", ReadOperation()).plan.model_dump(mode="json")
    if field in {"batch_id", "workspace_id"}:
        plan[field] = str(uuid4())
    elif field == "members":
        plan[field] = plan[field][::-1]
    else:
        plan[field] = "changed"
    # 重算外层摘要也不能使错绑成员或额外字段合法。
    plan["approval_fingerprint"] = digest(
        {k: v for k, v in plan.items() if k != "approval_fingerprint"}
    )
    with pytest.raises(ValidationError):
        ManagedPatchBatchPlan.model_validate_json(json.dumps(plan))


@pytest.mark.parametrize(
    "kind",
    ["group_checksum", "approval_checksum", "request", "owner", "body", "member_event", "decision"],
)
def test_corrupt_group_cannot_be_read_as_valid(group_case, kind):
    _, _, copy, groups, batch = group_case
    plan = groups.save(batch, "stable", ReadOperation()).plan
    groups.reply(plan.batch_id, plan.approval_fingerprint, APPROVE, ReadOperation())
    if kind == "group_checksum":
        copy._db.execute("UPDATE batches SET checksum='bad'")
    elif kind == "approval_checksum":
        copy._db.execute("UPDATE batch_approvals SET checksum='bad'")
    elif kind == "request":
        copy._db.execute("UPDATE batches SET request_id='other'")
    elif kind == "owner":
        copy._db.execute(
            "UPDATE plans SET owner_batch_id=NULL WHERE id=?", (str(plan.members[0].plan_id),)
        )
    elif kind == "body":
        copy._db.execute("UPDATE plans SET after_image=?", (b"corrupt",))
    elif kind == "member_event":
        copy._db.execute("UPDATE events SET checksum='bad'")
    else:
        payload = '{"outcome":"invalid","actor":"test"}'
        copy._db.execute(
            "UPDATE batch_approvals SET payload=?,checksum=?",
            (payload, digest((str(plan.batch_id), plan.approval_fingerprint, payload))),
        )
    with pytest.raises(KernelError):
        groups.get(plan.batch_id, ReadOperation())


@pytest.mark.parametrize("position", range(3))
def test_null_owner_column_does_not_open_single_write_path(group_case, position):
    _, _, copy, groups, batch = group_case
    plan = groups.save(batch, "stable", ReadOperation()).plan
    member = plan.members[position]
    copy._db.execute("UPDATE plans SET owner_batch_id=NULL WHERE id=?", (str(member.plan_id),))
    for action in (
        lambda: copy.reply(member.plan_id, member.approval_fingerprint, APPROVE),
        lambda: copy.execute(member.plan_id, member.approval_fingerprint, ReadOperation()),
        lambda: copy.save(batch.patches[position], member.request_id, ReadOperation()),
    ):
        with failure("patch_ledger_corrupt"):
            action()
    assert counts(copy) == (3, 3, 1, 0)


@pytest.mark.parametrize("cut", ["approval_before_commit", "approval_committed"])
@pytest.mark.parametrize("kind", ["storage", "cancel", "timeout"])
def test_reply_failure_and_lost_ack_are_resolved_by_reading(group_case, monkeypatch, cut, kind):
    _, _, copy, groups, batch = group_case
    plan = groups.save(batch, "stable", ReadOperation()).plan
    operation = ReadOperation()

    def fault(point):
        if point == cut:
            if kind == "storage":
                raise sqlite3.OperationalError("提交切点故障")
            if kind == "cancel":
                operation.stopped.set()
            else:
                operation.deadline = 0
            operation.checkpoint()

    monkeypatch.setattr(managed_batches, "_fault", fault)
    with pytest.raises(TurnCancelled if kind == "cancel" else KernelError):
        groups.reply(plan.batch_id, plan.approval_fingerprint, APPROVE, operation)
    result = groups.lookup("stable", ReadOperation())
    assert result.decision == (APPROVE if cut == "approval_committed" else None)
    assert counts(copy) == (3, 3, 1, int(cut == "approval_committed"))
    assert not copy._db.in_transaction


def test_borrowed_workspace_lock_serializes_independent_group_handles(group_case):
    _, _, copy, _, batch = group_case

    def save(_):
        return ManagedPatchBatches(copy).save(batch, "stable", ReadOperation())

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(8)))
    assert all(result == results[0] for result in results)
    assert counts(copy) == (3, 3, 1, 0)
    plan = results[0].plan

    def reply(decision):
        try:
            return ManagedPatchBatches(copy).reply(
                plan.batch_id, plan.approval_fingerprint, decision, ReadOperation()
            )
        except KernelError as error:
            assert error.code == "patch_approval_conflict"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reply, (APPROVE, REJECT)))
    assert sum(result is not None for result in results) == 1
    assert counts(copy) == (3, 3, 1, 1)


@pytest.mark.parametrize("table,limit", [("batches", 64 * 1024), ("batch_approvals", 16 * 1024)])
def test_oversized_persistent_payload_is_refused(group_case, table, limit):
    _, _, copy, groups, batch = group_case
    plan = groups.save(batch, "stable", ReadOperation()).plan
    groups.reply(plan.batch_id, plan.approval_fingerprint, APPROVE, ReadOperation())
    payload = "界" * (limit // 3 + 1)
    copy._db.execute(f"UPDATE {table} SET payload=?", (payload,))
    with failure("patch_ledger_corrupt"):
        groups.get(plan.batch_id, ReadOperation())


def test_group_payload_cannot_move_between_copies(group_case):
    source, factory, _, _, batch = group_case
    with factory.create(source, PATHS, ReadOperation()) as another:
        with pytest.raises(KernelError):
            ManagedPatchBatches(another).save(batch, "stable", ReadOperation())
        assert counts(another) == (0, 0, 0, 0)


@pytest.mark.parametrize("action", ["save", "reply"])
def test_storage_readonly_never_leaves_partial_group(group_case, action):
    _, _, copy, groups, batch = group_case
    if action == "reply":
        plan = groups.save(batch, "stable", ReadOperation()).plan
    before = counts(copy)
    copy._db.execute("PRAGMA query_only=ON")
    with failure("patch_storage_unavailable"):
        if action == "save":
            groups.save(batch, "stable", ReadOperation())
        else:
            groups.reply(plan.batch_id, plan.approval_fingerprint, APPROVE, ReadOperation())
    assert counts(copy) == before


def test_group_plan_schema_rejects_invalid_decision_before_commit(group_case):
    _, _, copy, groups, batch = group_case
    plan = groups.save(batch, "stable", ReadOperation()).plan
    decision = APPROVE.model_copy(update={"actor": "x" * 257})
    with pytest.raises(ValidationError):
        groups.reply(plan.batch_id, plan.approval_fingerprint, decision, ReadOperation())
    assert counts(copy) == (3, 3, 1, 0)
