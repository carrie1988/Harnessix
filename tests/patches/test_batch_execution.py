import json
import os
import sqlite3
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import batch_execution, ledger, managed
from harnessix.patches.batch_run_contracts import BatchExecutionResult, BatchRunRecord
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.workspace import ReadOperation, Workspace, digest
from tests.patches.test_managed import APPROVE, REJECT, failure, set_xattr
from tests.patches.test_managed_batches import PATHS, prepare, snapshot
from tests.patches.test_managed_batches import group_case as group_case

BEFORE = ("started", "temp_created", "temp_synced", "temp_recorded", "before_replace")
AFTER = ("after_replace", "directories_synced", "before_result", "result_recorded")


def approved(groups, batch):
    pending = groups.save(batch, "execute", ReadOperation())
    return groups.reply(
        pending.plan.batch_id, pending.plan.approval_fingerprint, APPROVE, ReadOperation()
    ).plan


def events(copy):
    return copy._db.execute("SELECT * FROM batch_run_events ORDER BY sequence").fetchall()


def no_write(*args, **kwargs):
    raise AssertionError("核对不能调用写执行入口")


def test_full_group_execution_and_reopen_are_once_only(group_case, monkeypatch):
    source, factory, copy, groups, batch = group_case
    original = snapshot(source.root)
    plan = approved(groups, batch)
    assert groups.get_execution(plan.batch_id, ReadOperation()) is None
    assert groups.reconcile(plan.batch_id, ReadOperation()) is None
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.run.phase == "finished" and result.run.stop_reason == "completed"
    assert result.effect == "applied" and [m.state for m in result.members] == ["applied"] * 3
    assert [m.plan_id for m in result.members] == [m.plan_id for m in plan.members]
    assert len(events(copy)) == 2
    assert groups.save(batch, "execute", ReadOperation()).plan == plan
    assert all((copy.workspace.root / path).read_bytes() == b"after\r\n" for path in PATHS)
    changed = snapshot(copy.workspace.root)
    copy.close()
    monkeypatch.setattr(managed.ManagedPatchWorkspace, "_execute", no_write)
    with factory.open(copy.workspace_id) as reopened:
        groups = ManagedPatchBatches(reopened)
        assert groups.get_execution(plan.batch_id, ReadOperation()) == result
        assert groups.reconcile(plan.batch_id, ReadOperation()) == result
        with failure("patch_not_executable"):
            groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
        assert snapshot(reopened.workspace.root) == changed
        assert len(events(reopened)) == 2
        for member in plan.members:
            with failure("patch_batch_member_requires_group"):
                reopened.reconcile(member.plan_id, ReadOperation())
    assert snapshot(source.root) == original


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("cut", BEFORE + AFTER)
@pytest.mark.parametrize("kind", ["storage", "cancel", "timeout"])
def test_member_failure_stops_suffix_and_preserves_effect(
    group_case, monkeypatch, position, cut, kind
):
    source, _, copy, groups, batch = group_case
    original, before = snapshot(source.root), snapshot(copy.workspace.root)
    plan = approved(groups, batch)
    operation = ReadOperation()
    current = -1

    def fault(point):
        nonlocal current
        if point == "started":
            current += 1
        if current == position and point == cut:
            if kind == "storage":
                raise OSError("文件写切点故障")
            if kind == "cancel":
                operation.stopped.set()
            else:
                operation.deadline = 0

    monkeypatch.setattr(managed, "_fault", fault)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, operation)
    assert (
        result.run.stop_reason
        == {"storage": "failed", "cancel": "cancelled", "timeout": "timeout"}[kind]
    )
    member_state = (
        "failed"
        if cut in BEFORE
        else "uncertain"
        if kind == "storage" and cut != "result_recorded"
        else "applied"
    )
    assert [m.state for m in result.members] == ["applied"] * position + [member_state] + [
        "pending"
    ] * (2 - position)
    expected_applied = position + int(cut in AFTER)
    expected_effect = (
        "unknown"
        if member_state == "uncertain"
        else "applied"
        if expected_applied == 3
        else "partial"
        if expected_applied
        else "not_applied"
    )
    assert result.effect == expected_effect
    for index, path in enumerate(PATHS):
        assert (copy.workspace.root / path).read_bytes() == (
            b"after\r\n" if index < expected_applied else b"before\r\n"
        )
    assert snapshot(copy.workspace.root)[expected_applied:] == before[expected_applied:]
    observed_before = snapshot(copy.workspace.root)
    monkeypatch.setattr(copy, "_execute", no_write)
    recovered = groups.reconcile(plan.batch_id, ReadOperation())
    assert recovered.run.stop_reason == result.run.stop_reason
    assert recovered.effect == (
        "applied" if expected_applied == 3 else "partial" if expected_applied else "not_applied"
    )
    assert groups.reconcile(plan.batch_id, ReadOperation()) == recovered
    with failure("patch_not_executable"):
        groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert snapshot(copy.workspace.root) == observed_before
    assert snapshot(source.root) == original


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("kind", ["body", "metadata"])
def test_whole_group_preflight_consumes_without_any_file_write(group_case, position, kind):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    target = copy.workspace.root / PATHS[position]
    if kind == "body":
        target.write_bytes(b"drift")
    else:
        set_xattr(target)
    before = snapshot(copy.workspace.root)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.run.stop_reason == "failed" and result.effect == "not_applied"
    assert all(member.state == "pending" for member in result.members)
    assert snapshot(copy.workspace.root) == before
    with failure("patch_not_executable"):
        groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())


@pytest.mark.parametrize("decision", [None, REJECT])
def test_unapproved_group_never_starts(group_case, decision):
    _, _, copy, groups, batch = group_case
    plan = groups.save(batch, "pending", ReadOperation()).plan
    if decision:
        groups.reply(plan.batch_id, plan.approval_fingerprint, decision, ReadOperation())
    with failure("patch_not_executable"):
        groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert groups.reconcile(plan.batch_id, ReadOperation()) is None
    assert events(copy) == []


def test_group_and_single_approval_fingerprints_are_not_interchangeable(group_case):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    for fingerprint in ("0" * 64, plan.members[0].approval_fingerprint):
        with failure("patch_approval_mismatch"):
            groups.execute(plan.batch_id, fingerprint, ReadOperation())
    assert events(copy) == []


@pytest.mark.parametrize("method", ["execute", "get_execution", "reconcile"])
@pytest.mark.parametrize("kind", ["cancel", "timeout"])
def test_cancelled_before_consumption_does_not_create_run(group_case, method, kind):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    operation = ReadOperation()
    if kind == "cancel":
        operation.stopped.set()
    else:
        operation.deadline = 0
    with pytest.raises(TurnCancelled if kind == "cancel" else KernelError):
        if method == "execute":
            groups.execute(plan.batch_id, plan.approval_fingerprint, operation)
        else:
            getattr(groups, method)(plan.batch_id, operation)
    assert events(copy) == []


@pytest.mark.parametrize(
    "cut", ["run_before_commit", "run_started", "run_result_before_commit", "run_result_committed"]
)
def test_group_storage_failure_never_allows_reexecution(group_case, monkeypatch, cut):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)

    def fault(point):
        if point == cut:
            raise sqlite3.OperationalError("组运行事务故障")

    monkeypatch.setattr(batch_execution, "_fault", fault)
    if cut == "run_started":
        result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
        assert result.effect == "not_applied" and result.run.stop_reason == "failed"
    else:
        with failure("patch_storage_unavailable"):
            groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    current = groups.get_execution(plan.batch_id, ReadOperation())
    if cut == "run_before_commit":
        assert current is None and events(copy) == []
    else:
        before = snapshot(copy.workspace.root)
        monkeypatch.setattr(batch_execution, "_fault", lambda point: None)
        recovered = groups.reconcile(plan.batch_id, ReadOperation())
        assert recovered.run.stop_reason == (
            "failed"
            if cut == "run_started"
            else "completed"
            if cut == "run_result_committed"
            else "interrupted"
        )
        assert snapshot(copy.workspace.root) == before
        with failure("patch_not_executable"):
            groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())


@pytest.mark.parametrize("position", range(3))
def test_identical_bytes_without_recorded_inode_stay_unknown(group_case, monkeypatch, position):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    current = -1

    def fault(point):
        nonlocal current
        if point == "started":
            current += 1
        if current == position and point == "after_replace":
            target = copy.workspace.root / PATHS[position]
            other = target.with_name("replacement")
            other.write_bytes(target.read_bytes())
            other.chmod(0o640)
            os.replace(other, target)

    monkeypatch.setattr(managed, "_fault", fault)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.effect == "unknown" and result.members[position].state == "uncertain"
    before = snapshot(copy.workspace.root)
    assert groups.reconcile(plan.batch_id, ReadOperation()) == result
    assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize(
    "kind", ["checksum", "fingerprint", "workspace", "missing_start", "member_decision", "order"]
)
def test_corrupt_execution_facts_close_group_access(group_case, monkeypatch, kind):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    monkeypatch.setattr(batch_execution, "_fault", lambda point: None)
    groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    if kind == "checksum":
        copy._db.execute("UPDATE batch_run_events SET checksum='bad'")
    elif kind in {"fingerprint", "workspace"}:
        rows = copy._db.execute("SELECT sequence,payload FROM batch_run_events").fetchall()
        for sequence, payload in rows:
            value = json.loads(payload)
            value["approval_fingerprint" if kind == "fingerprint" else "workspace_id"] = (
                "0" * 64 if kind == "fingerprint" else str(uuid4())
            )
            serialized = json.dumps(value)
            copy._db.execute(
                "UPDATE batch_run_events SET payload=?,checksum=? WHERE sequence=?",
                (serialized, digest(serialized), sequence),
            )
    elif kind == "missing_start":
        copy._db.execute("DELETE FROM batch_run_events WHERE phase='started'")
    elif kind == "member_decision":
        copy._db.execute(
            "UPDATE batch_approvals SET payload=?,checksum=?",
            (
                REJECT.model_dump_json(),
                digest((str(plan.batch_id), plan.approval_fingerprint, REJECT.model_dump_json())),
            ),
        )
    else:
        copy._db.execute(
            "DELETE FROM events WHERE plan_id=? AND sequence NOT IN "
            "(SELECT min(sequence) FROM events GROUP BY plan_id)",
            (str(plan.members[0].plan_id),),
        )
    for method in (groups.get_execution, groups.reconcile):
        with pytest.raises(KernelError):
            method(plan.batch_id, ReadOperation())


def test_result_schema_rejects_effect_lies(group_case):
    _, _, _, groups, batch = group_case
    plan = approved(groups, batch)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    with pytest.raises(ValidationError):
        BatchExecutionResult.model_validate_json(
            result.model_copy(update={"effect": "not_applied"}).model_dump_json()
        )
    with pytest.raises(ValidationError):
        BatchRunRecord.model_validate_json(
            result.run.model_copy(update={"phase": "started"}).model_dump_json()
        )


@pytest.mark.parametrize("position", [1, 2])
def test_source_drift_between_members_stops_before_next_intent(group_case, monkeypatch, position):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)

    def fault(point):
        if point == f"member_completed:{position - 1}":
            (copy.workspace.root / PATHS[position]).write_bytes(b"drift")

    monkeypatch.setattr(batch_execution, "_fault", fault)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.effect == "partial" and result.run.stop_reason == "failed"
    assert [m.state for m in result.members] == ["applied"] * position + ["approved"] + [
        "pending"
    ] * (2 - position)
    before = snapshot(copy.workspace.root)
    assert groups.reconcile(plan.batch_id, ReadOperation()) == result
    assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("observation", ["missing", "diverged", "unavailable"])
def test_unknown_recovery_does_not_claim_absence_of_effect(group_case, monkeypatch, observation):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    current = -1

    def fault(point):
        nonlocal current
        if point == "started":
            current += 1
        if current == 1 and point == "after_replace":
            raise OSError("效果已经发起，但未完成归因")

    monkeypatch.setattr(managed, "_fault", fault)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.effect == "unknown"
    target = copy.workspace.root / PATHS[1]
    if observation == "diverged":
        target.write_bytes(b"different")
    else:
        target.unlink()
        if observation == "unavailable":
            target.symlink_to("missing")

    def identity_times():
        if observation == "missing":
            assert not target.exists()
            return None
        info = target.lstat()
        return info.st_ino, info.st_mtime_ns, info.st_ctime_ns

    before = identity_times()
    recovered = groups.reconcile(plan.batch_id, ReadOperation())
    assert recovered.effect == "unknown" and recovered.members[1].state == observation
    assert recovered.members[2].state == "pending"
    assert identity_times() == before


@pytest.mark.parametrize("kind", ["cancel", "timeout"])
def test_reconcile_budget_does_not_reopen_consumed_approval(group_case, monkeypatch, kind):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)

    def fault(point):
        if point == "after_replace":
            raise OSError("替换后的故障")

    monkeypatch.setattr(managed, "_fault", fault)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.effect == "unknown"
    operation = ReadOperation()

    def stop(point):
        if point == "member_reconciled:0":
            if kind == "cancel":
                operation.stopped.set()
            else:
                operation.deadline = 0

    before = snapshot(copy.workspace.root)
    monkeypatch.setattr(batch_execution, "_fault", stop)
    with pytest.raises(TurnCancelled if kind == "cancel" else KernelError):
        groups.reconcile(plan.batch_id, operation)
    current = groups.get_execution(plan.batch_id, ReadOperation())
    assert current.members[0].state == "observed_after" and current.effect == "partial"
    assert current.run.stop_reason == "failed"
    with failure("patch_not_executable"):
        groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("stage", ["before", "after"])
def test_member_ledger_failure_does_not_schedule_next(group_case, monkeypatch, position, stage):
    _, _, copy, groups, batch = group_case
    plan = approved(groups, batch)
    real_append = ledger.append

    def append(db, record, temporary):
        if record.plan_id == plan.members[position].plan_id and record.state in (
            {"started"} if stage == "before" else {"applied", "uncertain"}
        ):
            raise sqlite3.OperationalError("成员结果不可落库")
        real_append(db, record, temporary)

    monkeypatch.setattr(ledger, "append", append)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert result.run.stop_reason == "failed"
    assert result.members[position].state == ("approved" if stage == "before" else "started")
    assert all(m.state == "pending" for m in result.members[position + 1 :])
    if stage == "after":
        assert result.effect == "unknown"
    before = snapshot(copy.workspace.root)
    recovered = groups.reconcile(plan.batch_id, ReadOperation())
    assert recovered.effect != "unknown" and recovered.run.stop_reason == "failed"
    assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("stage", ["before", "after"])
def test_real_fsync_error_is_attributed_per_member(group_case, monkeypatch, position, stage):
    _, _, _, groups, batch = group_case
    plan = approved(groups, batch)
    current, after, failed = -1, False, False
    real_sync = os.fsync

    def fault(point):
        nonlocal current, after
        if point == "started":
            current += 1
            after = False
        if point == "after_replace":
            after = True

    def fsync(fd):
        nonlocal failed
        if current == position and after == (stage == "after") and not failed:
            failed = True
            raise OSError("真实 fsync 调用失败")
        return real_sync(fd)

    monkeypatch.setattr(managed, "_fault", fault)
    monkeypatch.setattr(os, "fsync", fsync)
    result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
    assert failed and result.run.stop_reason == "failed"
    assert result.members[position].state == ("failed" if stage == "before" else "uncertain")
    assert all(m.state == "pending" for m in result.members[position + 1 :])
    assert groups.reconcile(plan.batch_id, ReadOperation()).effect != "unknown"


def test_maximum_sixteen_member_batch_runs_in_order(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    paths = tuple(f"{i:02}.py" for i in range(16))
    for path in paths:
        (source / path).write_bytes(b"before")
    factory = managed.PatchWorkspaces(tmp_path / "private")
    with Workspace(source) as workspace, factory.create(workspace, paths, ReadOperation()) as copy:
        groups = ManagedPatchBatches(copy)
        plan = approved(groups, prepare(copy, paths))
        result = groups.execute(plan.batch_id, plan.approval_fingerprint, ReadOperation())
        assert result.run.stop_reason == "completed" and result.effect == "applied"
        assert [m.plan_id for m in result.members] == [m.plan_id for m in plan.members]
        assert all((source / path).read_bytes() == b"before" for path in paths)
        assert all((copy.workspace.root / path).read_bytes() == b"after" for path in paths)
