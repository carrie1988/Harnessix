from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import new_transaction_record, transition_transaction_record
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_record_contracts import MAX_STORED_RECORD_BYTES
from harnessix.workspace.snapshot import verify_workspace_snapshot


def _prepared(workspace: Path):
    workspace.mkdir()
    (workspace / "target.txt").write_bytes(b"before\n")
    (workspace / "target.txt").chmod(0o644)
    return prepare_workspace_transaction(
        workspace,
        {"target.txt": DesiredWorkspaceFile(b"after\n", 0o644)},
        request_id="reference-record",
        now=datetime(2026, 10, 5, tzinfo=UTC),
    )


def test_plan_metadata_uses_verified_cas_without_opening_file_image_port(tmp_path, monkeypatch):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(_prepared(tmp_path / "workspace"))
        payload = store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
        expected = json.loads(payload)["plan_ref"]["sha256"]
        reads = []
        read_cas = store._read_blob

        def metadata_read(digest):
            reads.append(digest)
            return read_cas(digest)

        def forbidden_image(_digest):
            raise AssertionError("读取Plan元数据不能打开文件镜像端口")

        monkeypatch.setattr(store, "_read_blob", metadata_read)
        monkeypatch.setattr(store, "blob", forbidden_image)
        assert store.load(record.transaction_id) == record
        assert store.decode_payload(payload).record == record
        assert reads == [expected, expected]


def test_valid_rollback_reads_before_image_after_actual_root_binding(tmp_path, monkeypatch):
    from harnessix.delivery import filesystem
    from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
    from harnessix.workspace.leases import WorkspaceLeaseStore

    root = tmp_path / "workspace"
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        with WorkspaceLeaseStore(tmp_path / "leases.db") as leases:
            runtime = WorkspaceTransactionRuntime(store, leases)
            record = store.save(_prepared(root))
            lease = leases.acquire(record.plan.source.workspace_id, "reader-test", ttl_seconds=60)
            original = runtime.publish(
                record.transaction_id,
                root,
                approval_fingerprint=record.plan.fingerprint,
                lease=lease,
            )
            root_checked = False
            images = []
            capture = filesystem.capture_workspace_snapshot
            read_image = store.blob

            def actual_root(*args, **kwargs):
                nonlocal root_checked
                observed = capture(*args, **kwargs)
                assert observed.workspace_id == original.plan.source.workspace_id
                root_checked = True
                return observed

            def bound_image(digest):
                assert root_checked, "文件镜像读取必须晚于原根身份观察"
                images.append(digest)
                return read_image(digest)

            monkeypatch.setattr(filesystem, "capture_workspace_snapshot", actual_root)
            monkeypatch.setattr(store, "blob", bound_image)
            rollback = runtime.build_rollback(original.transaction_id, root, request_id="inverse")
            assert images[0] == original.plan.mutations[0].before.sha256
            assert rollback.state == "prepared"
            assert store.load(original.transaction_id) == original
            assert (root / "target.txt").read_bytes() == b"after\n"


def test_corrupt_plan_metadata_never_falls_back_to_file_image_port(tmp_path, monkeypatch):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(_prepared(tmp_path / "workspace"))
        payload = store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
        (state / "blobs" / json.loads(payload)["plan_ref"]["sha256"]).unlink()

        def forbidden_image(_digest):
            raise AssertionError("损坏Plan不能降级读取文件镜像")

        monkeypatch.setattr(store, "blob", forbidden_image)
        with pytest.raises(KernelError) as error:
            store.load(record.transaction_id)
        assert error.value.code == "delivery_store_corrupt"


def _long_prepared(workspace: Path, component_bytes: int):
    """通过真实相对句柄准备200叶；不以替代Snapshot制造长度反例。"""
    workspace.mkdir(mode=0o700)
    components = [f"d{i:02d}-" + "x" * (component_bytes - 4) for i in range(8)]
    desired = {}
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in components:
            os.mkdir(component, mode=0o700, dir_fd=directory)
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        for i in range(200):
            name = f"f{i:03d}.txt"
            leaf = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=directory,
            )
            try:
                os.fchmod(leaf, 0o644)
                assert os.write(leaf, b"before\n") == 7
            finally:
                os.close(leaf)
            desired["/".join([*components, name])] = DesiredWorkspaceFile(b"after\n", 0o644)
    finally:
        os.close(directory)
    return prepare_workspace_transaction(
        workspace,
        desired,
        request_id=f"reference-long-{component_bytes}",
        now=datetime(2026, 10, 5, tzinfo=UTC),
    )


@pytest.mark.skipif(os.name != "posix", reason="此长度反例使用POSIX原生相对句柄")
@pytest.mark.parametrize("component_bytes", [12, 180])
def test_real_long_plan_reopens_without_enlarging_record_limit(tmp_path, component_bytes):
    workspace = tmp_path / "workspace"
    prepared = _long_prepared(workspace, component_bytes)
    assert len(prepared.plan.mutations) == 200
    assert len(prepared.plan.source.resources) == 209
    assert sum(m.before.size + m.after.size for m in prepared.plan.mutations) == 2600
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(prepared)
        assert (len(record.model_dump_json().encode()) > 512 * 1024) == (component_bytes == 180)
        assert store.load(record.transaction_id) == record
        started = transition_transaction_record(
            record,
            state="publishing",
            cursor=0,
            now=datetime(2026, 10, 5, 1, tzinfo=UTC),
        )
        store.transition(record, started)
        interrupted = transition_transaction_record(
            started,
            state="interrupted",
            cursor=0,
            now=datetime(2026, 10, 5, 2, tzinfo=UTC),
        )
        store.transition(started, interrupted)
        payloads = [
            row[0]
            for row in store._db.execute(
                "SELECT payload FROM workspace_transaction_events ORDER BY sequence"
            )
        ]
        assert all(len(p.encode()) <= 512 * 1024 for p in payloads)
        references = [json.loads(p)["plan_ref"] for p in payloads]
        assert all(ref == references[0] for ref in references)
        assert len(tuple((state / "blobs").iterdir())) == len(prepared.blobs) + 1
        for payload in payloads:
            decoded = store.decode_payload(payload)
            assert decoded.record.plan == prepared.plan
            assert len(decoded.references) == 1
        assert store.save(prepared) == interrupted
    verify_workspace_snapshot(prepared.plan.source, workspace)
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as reopened:
        assert reopened.load(record.transaction_id) == interrupted


def test_old_inline_bytes_survive_readonly_and_version_upgrade(tmp_path):
    prepared = _prepared(tmp_path / "workspace")
    state = tmp_path / "state"
    record = new_transaction_record(prepared.plan)
    # 明确构造合法旧Schema1夹具，保存原v1字符串；不是缺失历史的自动迁移。
    original = record.model_dump_json(indent=2)
    with SQLiteWorkspaceTransactionStore(state) as store:
        store.save(prepared)
        store._db.execute("UPDATE delivery_metadata SET value='1' WHERE key='schema_version'")
        store._db.execute("UPDATE workspace_transactions SET payload=?", (original,))
        store._db.execute("UPDATE workspace_transaction_events SET payload=?", (original,))
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        assert store.load(record.transaction_id) == record
        assert store.decode_payload(original).references == ()
        assert store._db.execute("SELECT value FROM delivery_metadata").fetchone() == ("1",)
    with SQLiteWorkspaceTransactionStore(state) as store:
        assert store._db.execute("SELECT value FROM delivery_metadata").fetchone() == ("2",)
        assert store._db.execute("SELECT payload FROM workspace_transactions").fetchone() == (
            original,
        )
        started = transition_transaction_record(
            record,
            state="publishing",
            cursor=0,
            now=datetime(2026, 10, 5, 1, tzinfo=UTC),
        )
        store.transition(record, started)
        assert store.load(record.transaction_id) == started
        assert store._db.execute(
            "SELECT payload FROM workspace_transaction_events WHERE sequence=0"
        ).fetchone() == (original,)


@pytest.mark.parametrize(
    "damage", ["missing", "body", "size", "fingerprint", "version", "domain", "cross_plan"]
)
def test_reference_damage_is_rejected_by_formal_reader(tmp_path, damage):
    prepared = _prepared(tmp_path / "workspace")
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(prepared)
        payload = store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
        value = json.loads(payload)
        reference = value["plan_ref"]
        if damage == "missing":
            (state / "blobs" / reference["sha256"]).unlink()
        elif damage == "body":
            (state / "blobs" / reference["sha256"]).write_bytes(b"changed")
        elif damage == "size":
            reference["size"] += 1
        elif damage == "fingerprint":
            reference["fingerprint"] = "f" * 64
        elif damage == "version":
            value["spec_version"] = "harnessix.workspace-stored-record/v3"
        elif damage == "domain":
            value["domain_spec_version"] = "harnessix.workspace-transaction-record/v2"
        else:
            other = prepare_workspace_transaction(
                tmp_path / "workspace",
                {"target.txt": DesiredWorkspaceFile(b"other\n", 0o644)},
                request_id="other-request",
                now=datetime(2026, 10, 5, tzinfo=UTC),
            )
            other_record = store.save(other)
            other_payload = store._db.execute(
                "SELECT payload FROM workspace_transactions WHERE transaction_id=?",
                (str(other_record.transaction_id),),
            ).fetchone()[0]
            value["plan_ref"] = json.loads(other_payload)["plan_ref"]
        changed = json.dumps(value)
        store._db.execute(
            "UPDATE workspace_transactions SET payload=? WHERE transaction_id=?",
            (changed, str(record.transaction_id)),
        )
        store._db.execute(
            "UPDATE workspace_transaction_events SET payload=? WHERE transaction_id=?",
            (changed, str(record.transaction_id)),
        )
        with pytest.raises(KernelError) as error:
            store.load(record.transaction_id)
        assert error.value.code == "delivery_store_corrupt"


def test_readonly_store_does_not_write_new_reference(tmp_path):
    prepared = _prepared(tmp_path / "workspace")
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        store.save(prepared)
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state / "blobs").iterdir()
    }
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        with pytest.raises(KernelError) as error:
            store.save(prepared)
        assert error.value.code == "delivery_store_read_only"
    assert before == {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state / "blobs").iterdir()
    }


def test_readonly_unknown_schema_is_rejected(tmp_path):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state):
        pass
    with sqlite3.connect(state / "transactions.db") as db:
        db.execute("UPDATE delivery_metadata SET value='999' WHERE key='schema_version'")
    with pytest.raises(KernelError) as error:
        SQLiteWorkspaceTransactionStore(state, read_only=True)
    assert error.value.code == "delivery_store_version"


@pytest.mark.parametrize("inline", [True, False])
def test_reader_keeps_exact_original_utf8_limit(tmp_path, inline):
    prepared = _prepared(tmp_path / "workspace")
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(prepared)
        payload = (
            record.model_dump_json()
            if inline
            else store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
        )
        padded = payload + " " * (MAX_STORED_RECORD_BYTES - len(payload.encode("utf-8")))
        assert len(padded.encode("utf-8")) == MAX_STORED_RECORD_BYTES
        assert store.decode_payload(padded).record == record
        with pytest.raises(KernelError) as error:
            store.decode_payload(padded + " ")
        assert error.value.code == "delivery_store_corrupt"


def test_unconfirmed_plan_blob_never_commits_success(tmp_path, monkeypatch):
    from harnessix.delivery import store as implementation

    prepared = _prepared(tmp_path / "workspace")

    def fail_confirmation(*_):
        raise KernelError("delivery_storage_unavailable", "测试耐久确认失败")

    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        monkeypatch.setattr(implementation, "confirm_blob_durable", fail_confirmation)
        with pytest.raises(KernelError) as error:
            store.save(prepared)
        assert error.value.code == "delivery_storage_unavailable"
        assert store._db.execute("SELECT count(*) FROM workspace_transactions").fetchone() == (0,)
        assert store._db.execute(
            "SELECT count(*) FROM workspace_transaction_events"
        ).fetchone() == (0,)


def test_two_owners_do_not_overwrite_current_reference(tmp_path):
    prepared = _prepared(tmp_path / "workspace")
    state = tmp_path / "state"
    with (
        SQLiteWorkspaceTransactionStore(state) as first,
        SQLiteWorkspaceTransactionStore(state) as second,
    ):
        record = first.save(prepared)
        started = transition_transaction_record(
            record,
            state="publishing",
            cursor=0,
            now=datetime(2026, 10, 5, 1, tzinfo=UTC),
        )
        first.transition(record, started)
        with pytest.raises(KernelError) as error:
            second.transition(record, started)
        assert error.value.code == "delivery_transaction_stale"
        assert second.load(record.transaction_id) == started
        assert second._db.execute(
            "SELECT count(*) FROM workspace_transaction_events"
        ).fetchone() == (2,)


@pytest.mark.parametrize("version", ["transaction-record/v1", "stored-record/v2"])
def test_deep_payload_below_limit_is_classified_without_writes(tmp_path, version):
    prepared = _prepared(tmp_path / "workspace")
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(prepared)
        payload = (
            '{"spec_version":"harnessix.workspace-'
            + version
            + '","deep":'
            + "[" * 16000
            + "0"
            + "]" * 16000
            + "}"
        )
        assert len(payload.encode()) < MAX_STORED_RECORD_BYTES
        store._db.execute("UPDATE workspace_transactions SET payload=?", (payload,))
        store._db.execute("UPDATE workspace_transaction_events SET payload=?", (payload,))
    original = (state / "transactions.db").read_bytes()
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        with pytest.raises(KernelError) as error:
            store.load(record.transaction_id)
        assert error.value.code == "delivery_store_corrupt"
    assert (state / "transactions.db").read_bytes() == original


def test_transition_confirmation_failure_keeps_actual_sql_bytes(tmp_path, monkeypatch):
    from harnessix.delivery import store as implementation

    prepared = _prepared(tmp_path / "workspace")
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(prepared)
        before = store._db.execute("SELECT * FROM workspace_transactions").fetchall()
        history = store._db.execute("SELECT * FROM workspace_transaction_events").fetchall()
        updated = transition_transaction_record(
            record,
            state="publishing",
            cursor=0,
            now=datetime(2026, 10, 5, 1, tzinfo=UTC),
        )

        def fail_confirmation(*_):
            raise KernelError("delivery_storage_unavailable", "测试已有Plan耐久确认失败")

        monkeypatch.setattr(implementation, "confirm_blob_durable", fail_confirmation)
        with pytest.raises(KernelError) as error:
            store.transition(record, updated)
        assert error.value.code == "delivery_storage_unavailable"
        assert store._db.execute("SELECT * FROM workspace_transactions").fetchall() == before
        assert store._db.execute("SELECT * FROM workspace_transaction_events").fetchall() == history


def test_old_raw_record_two_owners_preserve_original_history(tmp_path):
    prepared = _prepared(tmp_path / "workspace")
    state = tmp_path / "state"
    record = new_transaction_record(prepared.plan)
    original = record.model_dump_json(indent=2)
    with SQLiteWorkspaceTransactionStore(state) as store:
        store.save(prepared)
        store._db.execute("UPDATE delivery_metadata SET value='1' WHERE key='schema_version'")
        store._db.execute("UPDATE workspace_transactions SET payload=?", (original,))
        store._db.execute("UPDATE workspace_transaction_events SET payload=?", (original,))
    with (
        SQLiteWorkspaceTransactionStore(state) as first,
        SQLiteWorkspaceTransactionStore(state) as second,
    ):
        assert first.load(record.transaction_id) == second.load(record.transaction_id) == record
        updated = transition_transaction_record(
            record,
            state="publishing",
            cursor=0,
            now=datetime(2026, 10, 5, 1, tzinfo=UTC),
        )
        first.transition(record, updated)
        with pytest.raises(KernelError) as error:
            second.transition(record, updated)
        assert error.value.code == "delivery_transaction_stale"
        assert first._db.execute(
            "SELECT payload FROM workspace_transaction_events WHERE sequence=0"
        ).fetchone() == (original,)
        assert second.load(record.transaction_id) == updated
