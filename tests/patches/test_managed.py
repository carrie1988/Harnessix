import errno
import json
import os
import sqlite3
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches import ledger, managed
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.patches.planner import prepare_patch
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace, digest

APPROVE = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="本地验收")
REJECT = ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="本地验收")


@contextmanager
def failure(code):
    with pytest.raises(KernelError) as error:
        yield
    assert error.value.code == code


def set_xattr(target):
    import ctypes
    import sys

    if sys.platform == "darwin":
        lib = ctypes.CDLL(None, use_errno=True)
        fn = lib.setxattr
        fn.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        fn.restype = ctypes.c_int
        assert fn(os.fsencode(target), b"user.harnessix", b"value", 5, 0, 0) == 0
    else:
        os.setxattr(target, "user.harnessix", b"value")


def plan(copy, request="request-1", old="before", new="after", path="nested/main.py"):
    revision = read_file(copy.workspace, ReadFileInput(path=path), ReadOperation()).revision
    prepared = prepare_patch(
        copy.workspace,
        PatchProposal(
            path=path,
            expected_revision=revision,
            edits=(ExactEdit(old_text=old, new_text=new),),
        ),
        ReadOperation(),
    )
    return prepared, copy.save(prepared, request, ReadOperation())


@pytest.fixture
def case(tmp_path):
    source_path = tmp_path / "source"
    source_path.mkdir()
    (source_path / "nested").mkdir()
    (source_path / "nested/main.py").write_bytes(b"before\r\n")
    (source_path / "nested/main.py").chmod(0o640)
    with ExitStack() as stack:
        source = stack.enter_context(Workspace(source_path))
        factory = PatchWorkspaces(tmp_path / "private")
        copy = stack.enter_context(factory.create(source, ["nested/main.py"], ReadOperation()))
        yield source, factory, copy


def approved(copy):
    _, record = plan(copy)
    return copy.reply(record.plan_id, record.approval_fingerprint, APPROVE)


def test_copy_approval_execution_reopen_source_unchanged(case):
    source, factory, copy = case
    prepared, record = plan(copy)
    target = copy.workspace.root / "nested/main.py"
    assert target.read_bytes() == b"before\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert copy.manifest.files[0].source_revision != prepared.manifest.source_revision
    with failure("patch_not_executable"):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert copy.reply(record.plan_id, record.approval_fingerprint, APPROVE).state == "approved"
    assert target.read_bytes() == prepared.before
    result = copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert result.state == "applied"
    assert target.read_bytes() == prepared.after == b"after\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert (source.root / "nested/main.py").read_bytes() == prepared.before
    assert copy.save(prepared, record.request_id, ReadOperation()) == result
    assert not list(copy._bundle.root.glob("*.patch"))
    copy.close()
    with factory.open(copy.workspace_id) as reopened:
        assert reopened.get(record.plan_id) == result
        assert reopened.reconcile(record.plan_id, ReadOperation()) == result
        assert reopened.reply(record.plan_id, record.approval_fingerprint, APPROVE) == result
        with failure("patch_not_executable"):
            reopened.execute(record.plan_id, record.approval_fingerprint, ReadOperation())


def test_reject_and_approval_binding(case):
    _, _, copy = case
    prepared, first = plan(copy)
    second = copy.save(prepared, "request-2", ReadOperation())
    assert first.approval_fingerprint != second.approval_fingerprint
    with failure("patch_approval_mismatch"):
        copy.reply(second.plan_id, first.approval_fingerprint, APPROVE)
    assert copy.reply(first.plan_id, first.approval_fingerprint, REJECT).state == "rejected"
    with failure("patch_approval_conflict"):
        copy.reply(first.plan_id, first.approval_fingerprint, APPROVE)
    with failure("patch_not_executable"):
        copy.execute(first.plan_id, first.approval_fingerprint, ReadOperation())
    with failure("patch_plan_not_found"):
        copy.get(uuid4())


def test_plan_idempotency_conflict_and_payload_validation(case):
    _, _, copy = case
    prepared, record = plan(copy)
    assert copy.save(prepared, record.request_id, ReadOperation()) == record
    with failure("patch_request_conflict"):
        plan(copy, new="another")
    with failure("patch_plan_corrupt"):
        copy.save(replace(prepared, after=b"tampered"), "other", ReadOperation())
    assert copy.get(record.plan_id).state == "pending"


@pytest.mark.parametrize("limit", ["MAX_COPY_PLANS", "MAX_PLAN_BYTES"])
def test_plan_quotas(case, monkeypatch, limit):
    _, _, copy = case
    monkeypatch.setattr(ledger, limit, 1)
    if limit == "MAX_COPY_PLANS":
        prepared, record = plan(copy)
        assert copy.save(prepared, record.request_id, ReadOperation()) == record
    with failure("patch_limit_exceeded"):
        plan(copy, request="second")


def test_unregistered_target_cannot_be_saved(case):
    _, _, copy = case
    (copy.workspace.root / "unregistered.py").write_text("before")
    with failure("patch_path_denied"):
        plan(copy, path="unregistered.py")


def test_single_owner_and_closed_handle(case):
    _, factory, copy = case
    with failure("patch_workspace_busy"):
        factory.open(copy.workspace_id)
    record = approved(copy)
    copy.close()
    copy.close()
    with failure("patch_workspace_closed"):
        copy.get(record.plan_id)
    with factory.open(copy.workspace_id) as reopened:
        assert reopened.get(record.plan_id).state == "approved"


@pytest.mark.parametrize("path", [".env", ".git/config", "../escape", ".", "/absolute"])
def test_copy_denied_paths(case, path):
    source, factory, _ = case
    with failure("patch_path_denied"):
        factory.create(source, [path], ReadOperation())


def test_overlap_and_non_private_root(case, tmp_path):
    source, _, _ = case
    nested = PatchWorkspaces(source.root / "private")
    with failure("patch_source_overlap"):
        nested.create(source, ["nested/main.py"], ReadOperation())
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with failure("patch_private_path_required"):
        PatchWorkspaces(public)


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "binary", "large", "special_mode"])
def test_unsupported_import_is_quarantined(case, kind):
    source, factory, _ = case
    target = source.root / "nested/main.py"
    if kind == "hardlink":
        os.link(target, source.root / "alias")
    elif kind == "symlink":
        target.unlink()
        target.symlink_to("missing")
    elif kind == "binary":
        target.write_bytes(b"before\0")
    elif kind == "large":
        target.write_bytes(b"a" * (1024 * 1024 + 1))
    else:
        target.chmod(0o4640)
    before = set(factory.root.iterdir())
    with pytest.raises(KernelError):
        factory.create(source, ["nested/main.py"], ReadOperation())
    building = (set(factory.root.iterdir()) - before).pop()
    from uuid import UUID

    with failure("patch_copy_not_ready"):
        factory.open(UUID(building.name))


@pytest.mark.parametrize("kind", ["content", "mode", "hardlink", "symlink", "xattr"])
def test_target_drift_refused_before_intent(case, kind):
    _, _, copy = case
    record = approved(copy)
    target = copy.workspace.root / "nested/main.py"
    if kind == "content":
        target.write_text("editor")
    elif kind == "mode":
        target.chmod(0o600)
    elif kind == "hardlink":
        os.link(target, copy.workspace.root / "alias")
    elif kind == "symlink":
        target.unlink()
        target.symlink_to("missing")
    else:
        set_xattr(target)
    with pytest.raises(KernelError):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert copy.get(record.plan_id).state == "approved"
    assert copy._db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


@pytest.mark.parametrize("name", ["workspace", "owner.lock", "ledger.sqlite"])
def test_replaced_private_identity_refused(case, name):
    _, factory, copy = case
    record = approved(copy)
    path = copy._bundle.root / name
    path.rename(path.with_name(name + ".old"))
    if name == "workspace":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(path.with_name(name + ".old").read_bytes())
        path.chmod(0o600)
    with pytest.raises(KernelError):
        copy.get(record.plan_id)
    copy.close()
    with pytest.raises(KernelError):
        factory.open(copy.workspace_id)


@pytest.mark.parametrize(
    "point", ["started", "temp_created", "temp_synced", "temp_recorded", "before_replace"]
)
def test_cancel_before_replace_is_consumed_failed(case, monkeypatch, point):
    _, _, copy = case
    record = approved(copy)
    operation = ReadOperation()
    monkeypatch.setattr(
        managed, "_fault", lambda cut: operation.stopped.set() if cut == point else None
    )
    result = copy.execute(record.plan_id, record.approval_fingerprint, operation)
    assert result.state == "failed"
    assert result.error_code == "cancelled"
    assert (copy.workspace.root / "nested/main.py").read_text() == "before\n"
    assert not list(copy._bundle.root.glob("*.patch"))
    with failure("patch_not_executable"):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())


def test_already_cancelled_does_not_consume_approval(case):
    _, _, copy = case
    record = approved(copy)
    operation = ReadOperation()
    operation.stopped.set()
    with pytest.raises(TurnCancelled):
        copy.execute(record.plan_id, record.approval_fingerprint, operation)
    assert copy.get(record.plan_id).state == "approved"


def test_cancel_after_replace_drains_to_applied(case, monkeypatch):
    _, _, copy = case
    record = approved(copy)
    operation = ReadOperation()
    monkeypatch.setattr(
        managed, "_fault", lambda cut: operation.stopped.set() if cut == "after_replace" else None
    )
    assert copy.execute(record.plan_id, record.approval_fingerprint, operation).state == "applied"


@pytest.mark.parametrize("kind", ["short", "zero", "full"])
def test_write_failures_and_short_write_loop(case, monkeypatch, kind):
    _, _, copy = case
    record = approved(copy)
    real_write = os.write

    def write(fd, body):
        if kind == "short":
            return real_write(fd, body[:1])
        if kind == "zero":
            return 0
        raise OSError(errno.ENOSPC, "secret-path-must-not-escape")

    monkeypatch.setattr(os, "write", write)
    result = copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert result.state == ("applied" if kind == "short" else "failed")
    assert "secret-path" not in result.model_dump_json()
    assert not list(copy._bundle.root.glob("*.patch"))


@pytest.mark.parametrize("change", ["content", "parent", "mode"])
def test_late_drift_never_overwritten(case, monkeypatch, change):
    _, _, copy = case
    record = approved(copy)
    target = copy.workspace.root / "nested/main.py"

    def fault(point):
        if point != "before_replace":
            return
        if change == "parent":
            target.parent.rename(copy.workspace.root / "moved")
            target.parent.mkdir(mode=0o700)
            target.write_text("external")
        elif change == "content":
            target.write_text("external")
        else:
            target.chmod(0o600)

    monkeypatch.setattr(managed, "_fault", fault)
    assert (
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation()).state == "failed"
    )
    assert target.read_text() != "after\n"


@pytest.mark.parametrize(
    "observation",
    ["observed_after", "observed_before", "diverged", "missing", "unavailable", "uncertain"],
)
def test_uncertain_effect_observation_never_reexecutes(case, monkeypatch, observation):
    _, factory, copy = case
    record = approved(copy)
    target = copy.workspace.root / "nested/main.py"

    def fault(point):
        if point == ("before_replace" if observation == "observed_before" else "after_replace"):
            if observation == "observed_before":
                return
            raise OSError("injected")

    monkeypatch.setattr(managed, "_fault", fault)
    if observation == "observed_before":

        def no_replace(*args, **kwargs):
            raise OSError("ambiguous syscall")

        monkeypatch.setattr(os, "replace", no_replace)
    result = copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert result.state == "uncertain"
    if observation == "diverged":
        target.write_text("third")
    elif observation == "missing":
        target.unlink()
    elif observation == "unavailable":
        target.unlink()
        target.symlink_to("absent")
    elif observation == "uncertain":
        other = target.with_name("other")
        other.write_bytes(target.read_bytes())
        other.chmod(0o640)
        os.replace(other, target)
    copy.close()
    with factory.open(copy.workspace_id) as reopened:
        observed = reopened.reconcile(record.plan_id, ReadOperation())
        assert observed.state == observation
        assert reopened.reconcile(record.plan_id, ReadOperation()) == observed
        with failure("patch_not_executable"):
            reopened.execute(record.plan_id, record.approval_fingerprint, ReadOperation())


@pytest.mark.parametrize("kind", ["body", "proposal", "event", "binding", "baseline", "version"])
def test_ledger_corruption_refused(case, kind):
    _, factory, copy = case
    record = approved(copy)
    if kind == "body":
        copy._db.execute("UPDATE plans SET after_image=?", (b"corrupt",))
    elif kind == "proposal":
        copy._db.execute("UPDATE plans SET proposal='{}'")
    elif kind == "event":
        copy._db.execute("UPDATE events SET checksum='bad'")
    elif kind == "binding":
        row = copy._db.execute(
            "SELECT sequence,payload,temporary FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(row[1])
        payload["request_id"] = "another"
        serialized = json.dumps(payload)
        copy._db.execute(
            "UPDATE events SET payload=?,checksum=? WHERE sequence=?",
            (serialized, digest((serialized, row[2])), row[0]),
        )
    elif kind == "baseline":
        copy._db.execute("UPDATE baseline SET body=?", (b"corrupt",))
    else:
        copy._db.execute("PRAGMA user_version=99")
    if kind in {"baseline", "version"}:
        copy.close()
        with pytest.raises(KernelError):
            factory.open(copy.workspace_id)
    else:
        with pytest.raises(KernelError):
            copy.get(record.plan_id)


def test_sql_failure_before_intent_does_not_write(case):
    _, _, copy = case
    record = approved(copy)
    copy._db.execute("PRAGMA query_only=ON")
    with failure("patch_storage_unavailable"):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert (copy.workspace.root / "nested/main.py").read_text() == "before\n"
    assert copy.get(record.plan_id).state == "approved"


def test_result_storage_failure_reopens_as_started(case, monkeypatch):
    _, factory, copy = case
    record = approved(copy)
    real_append = ledger.append

    def append(db, item, temporary):
        if item.state in {"applied", "uncertain"}:
            raise sqlite3.OperationalError("private SQL")
        return real_append(db, item, temporary)

    monkeypatch.setattr(ledger, "append", append)
    with failure("patch_storage_unavailable"):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    copy.close()
    monkeypatch.setattr(ledger, "append", real_append)
    with factory.open(copy.workspace_id) as reopened:
        assert reopened.get(record.plan_id).state == "started"
        assert reopened.reconcile(record.plan_id, ReadOperation()).state == "observed_after"


def test_two_threads_cannot_execute_one_approval_twice(case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, _, copy = case
    record = approved(copy)
    barrier = Barrier(2)

    def execute():
        barrier.wait(timeout=5)
        try:
            return copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation()).state
        except KernelError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert sorted(results) == ["applied", "patch_not_executable"]


def test_close_waits_for_active_write(case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _, factory, copy = case
    record = approved(copy)
    entered, release, close_entered = Event(), Event(), Event()

    def fault(point):
        if point == "after_replace":
            entered.set()
            assert release.wait(timeout=5)

    def close():
        close_entered.set()
        copy.close()

    monkeypatch.setattr(managed, "_fault", fault)
    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(
            copy.execute, record.plan_id, record.approval_fingerprint, ReadOperation()
        )
        try:
            assert entered.wait(timeout=5)
            closing = pool.submit(close)
            assert close_entered.wait(timeout=5)
            assert not closing.done()
        finally:
            release.set()
        assert execution.result(timeout=5).state == "applied"
        closing.result(timeout=5)
    with factory.open(copy.workspace_id) as reopened:
        assert reopened.get(record.plan_id).state == "applied"


@pytest.mark.parametrize("kind", ["count", "bytes", "duplicates", "empty"])
def test_copy_quotas_and_explicit_selection(case, monkeypatch, kind):
    source, factory, _ = case
    paths = ["nested/main.py"]
    if kind == "count":
        monkeypatch.setattr(managed, "MAX_COPY_FILES", 0)
    elif kind == "bytes":
        monkeypatch.setattr(managed, "MAX_COPY_BYTES", 1)
    elif kind == "duplicates":
        paths *= 2
    else:
        paths = []
    with failure("patch_limit_exceeded"):
        factory.create(source, paths, ReadOperation())


@pytest.mark.parametrize("point", ["temp_synced", "directories_synced"])
def test_sync_failure_is_classified_by_replace_boundary(case, monkeypatch, point):
    _, _, copy = case
    record = approved(copy)

    def fail_sync(cut):
        if cut == point:
            raise OSError(errno.EIO, "private-device")

    monkeypatch.setattr(managed, "_fault", fail_sync)
    result = copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert result.state == ("failed" if point == "temp_synced" else "uncertain")
    assert "private-device" not in result.model_dump_json()
    assert copy.get(record.plan_id) == result


def test_real_fsync_error_after_replace_remains_uncertain(case, monkeypatch):
    _, _, copy = case
    record = approved(copy)

    def fault(point):
        if point == "after_replace":

            def fail_sync(fd):
                raise OSError(errno.EIO, "private-device")

            monkeypatch.setattr(os, "fsync", fail_sync)

    monkeypatch.setattr(managed, "_fault", fault)
    assert (
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation()).state
        == "uncertain"
    )
    assert copy.reconcile(record.plan_id, ReadOperation()).state == "observed_after"


def test_actual_metadata_check_rejects_extended_acl_on_darwin(case):
    import sys

    from harnessix.patches.managed_io import plain_metadata

    if sys.platform != "darwin":
        # Linux 的 POSIX ACL 使用 xattr，由现有属性用例和原生检查覆盖。
        return
    import ctypes

    _, _, copy = case
    record = approved(copy)
    target = copy.workspace.root / "nested/main.py"
    lib = ctypes.CDLL(None, use_errno=True)
    lib.acl_from_text.argtypes = [ctypes.c_char_p]
    lib.acl_from_text.restype = ctypes.c_void_p
    lib.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    lib.acl_free.argtypes = [ctypes.c_void_p]
    acl = lib.acl_from_text(
        b"!#acl 1\nuser:FFFFEEEE-DDDD-CCCC-BBBB-AAAA00000000:root:0:allow:read\n"
    )
    assert acl
    try:
        with target.open("rb") as file:
            assert lib.acl_set_fd_np(file.fileno(), acl, 0x100) == 0
            with failure("patch_unsupported_metadata"):
                plain_metadata(file.fileno())
    finally:
        lib.acl_free(acl)
    with pytest.raises(KernelError):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert copy.get(record.plan_id).state == "approved"


def test_metadata_inspection_failure_fails_closed(case, monkeypatch):
    from harnessix.patches import managed_io

    _, _, copy = case
    record = approved(copy)

    def failed(fd):
        raise OSError(errno.EACCES, "metadata-unavailable")

    monkeypatch.setattr(managed_io, "plain_metadata", failed)
    with failure("patch_storage_unavailable"):
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert copy.get(record.plan_id).state == "approved"


def test_new_request_needs_new_approval_after_observed_before(case, monkeypatch):
    _, _, copy = case
    prepared, record = plan(copy)
    copy.reply(record.plan_id, record.approval_fingerprint, APPROVE)

    def ambiguous(*args, **kwargs):
        raise OSError("unconfirmed replace")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", ambiguous)
        copy.execute(record.plan_id, record.approval_fingerprint, ReadOperation())
    assert copy.reconcile(record.plan_id, ReadOperation()).state == "observed_before"
    new = copy.save(prepared, "explicit-new-request", ReadOperation())
    assert new.state == "pending"
    with failure("patch_approval_mismatch"):
        copy.reply(new.plan_id, record.approval_fingerprint, APPROVE)
    copy.reply(new.plan_id, new.approval_fingerprint, APPROVE)
    assert copy.execute(new.plan_id, new.approval_fingerprint, ReadOperation()).state == "applied"
