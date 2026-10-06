"""Workspace物理引用备份：真实产品状态、完整事件历史和私有CAS闭合。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    WorkspaceTransactionRecord,
    transition_transaction_record,
    workspace_transaction_record_digest,
)
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import utc_now
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.product_config.state_backup import (
    _paths,
    backup_product_state,
    verify_product_backup,
)
from harnessix.product_config.state_backup_contracts import PROCESS_DATABASE
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.product_config.state_backup_validation import _schemas, validate_product_state
from harnessix.product_config.state_owner import state_owner_anchor
from harnessix.product_config.state_restore import restore_product_state
from harnessix.session.maintenance_io import MaintenanceIOControl
from tests.product_config.test_product_state_backup import complete_state as _complete_state

# 复用正式产品装配及离线Provider，不创建第二套数据库、认证身份或产物夹具。
complete_state = _complete_state

pytestmark = pytest.mark.skipif(
    os.name not in {"posix", "nt"}, reason="产品私有状态端口只支持POSIX和Windows"
)

WORKSPACE_DATABASE = "workspace-transactions/transactions.db"


@pytest.mark.parametrize("version", ["transaction-record/v1", "stored-record/v2"])
async def test_deep_history_only_payload_is_classified_before_backup_publication(
    reference_state, tmp_path, version
):
    root, records = reference_state
    payload = (
        '{"spec_version":"harnessix.workspace-'
        + version
        + '","deep":'
        + "[" * 16000
        + "0"
        + "]" * 16000
        + "}"
    )
    assert len(payload.encode()) < 512 * 1024
    with sqlite3.connect(root / WORKSPACE_DATABASE) as database:
        database.execute(
            "UPDATE workspace_transaction_events SET payload=? WHERE sequence=0", (payload,)
        )
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.load(records[-1].transaction_id) == records[-1]
        with pytest.raises(KernelError) as error:
            store.decode_payload(payload)
        assert error.value.code == "delivery_store_corrupt"
    original = (root / WORKSPACE_DATABASE).read_bytes()
    destination = tmp_path / "deep-backup"
    with pytest.raises(KernelError) as error:
        await backup_product_state(root, destination)
    assert error.value.code == "delivery_store_corrupt"
    _assert_unpublished(root, destination)
    assert (root / WORKSPACE_DATABASE).read_bytes() == original


def _advance_history(root, transaction_id):
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        records = [store.load(transaction_id)]
        for state in ("publishing", "interrupted", "publishing", "published"):
            current = records[-1]
            updated = transition_transaction_record(
                current,
                state=state,
                cursor=len(current.plan.mutations) if state == "published" else 0,
                now=utc_now(),
            )
            store.transition(current, updated)
            records.append(updated)
    return tuple(records)


@pytest.fixture
async def reference_state(complete_state):
    root, prepared = complete_state
    return root, _advance_history(root, prepared.plan.transaction_id)


def _events(root, transaction_id):
    with closing(sqlite3.connect(root / WORKSPACE_DATABASE)) as database:
        return database.execute(
            "SELECT sequence,state,payload FROM workspace_transaction_events "
            "WHERE transaction_id=? ORDER BY sequence",
            (str(transaction_id),),
        ).fetchall()


def _wire(root, record, sequence=0):
    wire = json.loads(_events(root, record.transaction_id)[sequence][2])
    assert wire["spec_version"] == "harnessix.workspace-stored-record/v2"
    assert wire["domain_spec_version"] == record.spec_version
    return wire


def _replace_event(root, record, sequence, wire):
    with sqlite3.connect(root / WORKSPACE_DATABASE) as database:
        database.execute(
            "UPDATE workspace_transaction_events SET payload=? "
            "WHERE transaction_id=? AND sequence=?",
            (json.dumps(wire), str(record.transaction_id), sequence),
        )


def _historical_plan_path(root, record):
    """用真实CAS保存同一完整Plan的另一种JSON字节，形成仅历史事件持有的引用。"""
    body = json.dumps(json.loads(record.plan.model_dump_json()), indent=2).encode()
    digest = hashlib.sha256(body).hexdigest()
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        store.put_blob(digest, body)
    wire = _wire(root, record)
    assert digest != wire["plan_ref"]["sha256"]
    wire["plan_ref"].update(sha256=digest, size=len(body))
    _replace_event(root, record, 0, wire)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        decoded = store.decode_payload(_events(root, record.transaction_id)[0][2])
        assert decoded.record == record
        assert decoded.references[0].sha256 == digest
    return "workspace-transactions/blobs/" + digest


def _validate_without_path(root, omitted):
    control = MaintenanceIOControl()
    with PrivateStateTree(root) as tree:
        paths = _paths(tree, control)
        assert omitted in paths
        validate_product_state(tree, tuple(path for path in paths if path != omitted), control)


def _assert_unpublished(root, destination):
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}.backup-*"))
    assert not list(state_owner_anchor(root).glob("backup-*.json"))


def _long_prepared(workspace):
    """原生相对句柄创建200个长路径叶，避免绝对路径长度影响真实Planner输入。"""
    workspace.mkdir(mode=0o700)
    components = [f"d{index:02d}-" + "p" * 176 for index in range(8)]
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
        for index in range(200):
            name = f"f{index:03d}.txt"
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory
            )
            try:
                os.fchmod(descriptor, 0o644)
                assert os.write(descriptor, b"before\n") == 7
            finally:
                os.close(descriptor)
            desired["/".join([*components, name])] = DesiredWorkspaceFile(b"after\n", 0o644)
    finally:
        os.close(directory)
    return prepare_workspace_transaction(
        workspace, desired, request_id="reference-backup-long-path", now=utc_now()
    )


@pytest.mark.parametrize(
    "long_plan",
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(os.name != "posix", reason="长路径夹具使用POSIX相对句柄")
        ),
    ],
)
async def test_real_reference_history_backup_verify_and_restore(
    reference_state, tmp_path, long_plan
):
    root, records = reference_state
    if long_plan:
        prepared = _long_prepared(tmp_path / "long-workspace")
        with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
            store.save(prepared)
        records = _advance_history(root, prepared.plan.transaction_id)
        assert len(records[0].model_dump_json().encode()) > 512 * 1024
        assert len(records[0].plan.mutations) == 200
        assert len(records[0].plan.source.resources) == 209
        assert sum(m.before.size + m.after.size for m in records[0].plan.mutations) == 2600

    history = _events(root, records[0].transaction_id)
    assert [row[1] for row in history] == [record.state for record in records]
    assert [row[0] for row in history] == list(range(len(records)))
    references = set()
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.load(records[-1].transaction_id) == records[-1]
        for row, record in zip(history, records, strict=True):
            assert len(row[2].encode()) <= 512 * 1024
            decoded = store.decode_payload(row[2])
            assert decoded.record == record
            assert len(decoded.references) == 1
            reference = decoded.references[0]
            assert reference.fingerprint == record.plan.fingerprint
            assert reference.size <= MAX_TRANSACTION_FILE_BYTES
            references.add((reference.sha256, reference.size))
    assert len(references) == 1

    historical_path = _historical_plan_path(root, records[0])
    history = _events(root, records[0].transaction_id)
    destination = tmp_path / "backup"
    manifest = await backup_product_state(root, destination)
    assert await verify_product_backup(root, destination) == manifest
    entries = {entry.path: entry for entry in manifest.files}
    assert historical_path in entries
    for digest, size in references:
        path = "workspace-transactions/blobs/" + digest
        assert (entries[path].size_bytes, entries[path].sha256) == (size, digest)
    for mutation in records[0].plan.mutations:
        for version in (mutation.before, mutation.after):
            if version.presence == "file":
                path = "workspace-transactions/blobs/" + version.sha256
                assert (entries[path].size_bytes, entries[path].sha256) == (
                    version.size,
                    version.sha256,
                )
    assert _events(destination / "state", records[0].transaction_id) == history

    added = b"post-backup-state"
    added_digest = hashlib.sha256(added).hexdigest()
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        store.put_blob(added_digest, added)
    result = await restore_product_state(
        root, destination, restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    assert result.status == "restored" and result.retained_previous_state
    assert not (root / "workspace-transactions/blobs" / added_digest).exists()
    assert _events(root, records[0].transaction_id) == history
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.load(records[-1].transaction_id) == records[-1]
        for row, record in zip(history, records, strict=True):
            assert store.decode_payload(row[2]).record == record
    assert await verify_product_backup(root, destination) == manifest


@pytest.mark.parametrize("location", ["current", "history-only"])
@pytest.mark.parametrize("damage", ["missing", "tamper"])
async def test_plan_blob_damage_never_publishes_backup(reference_state, tmp_path, location, damage):
    root, records = reference_state
    path = (
        _historical_plan_path(root, records[0])
        if location == "history-only"
        else "workspace-transactions/blobs/" + _wire(root, records[0])["plan_ref"]["sha256"]
    )
    blob = root / path
    if damage == "missing":
        blob.unlink()
    else:
        body = blob.read_bytes()
        blob.write_bytes(bytes([body[0] ^ 1]) + body[1:])
    destination = tmp_path / "backup"
    with pytest.raises(KernelError):
        await backup_product_state(root, destination)
    _assert_unpublished(root, destination)
    if damage == "missing":
        assert not blob.exists()


@pytest.mark.parametrize("location", ["current", "history-only", "mutation"])
async def test_every_reference_must_belong_to_original_inventory(reference_state, location):
    root, records = reference_state
    if location == "history-only":
        omitted = _historical_plan_path(root, records[0])
    elif location == "current":
        omitted = "workspace-transactions/blobs/" + _wire(root, records[0])["plan_ref"]["sha256"]
    else:
        omitted = "workspace-transactions/blobs/" + records[0].plan.mutations[0].after.sha256
    with pytest.raises(KernelError) as error:
        await asyncio.to_thread(_validate_without_path, root, omitted)
    assert error.value.code == "product_backup_state_invalid"


@pytest.mark.parametrize("field", ["sha256", "size", "fingerprint"])
async def test_historical_plan_reference_binding_is_verified(reference_state, tmp_path, field):
    root, records = reference_state
    wire = _wire(root, records[1], sequence=1)
    wire["plan_ref"][field] = wire["plan_ref"][field] + 1 if field == "size" else "0" * 64
    _replace_event(root, records[1], 1, wire)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.load(records[-1].transaction_id) == records[-1]
    destination = tmp_path / "backup"
    with pytest.raises(KernelError):
        await backup_product_state(root, destination)
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("field", ["spec_version", "domain_spec_version", "record_digest"])
async def test_invalid_historical_wire_is_rejected(reference_state, tmp_path, field):
    root, records = reference_state
    wire = _wire(root, records[1], sequence=1)
    wire[field] = "0" * 64 if field == "record_digest" else "harnessix.unknown/v999"
    _replace_event(root, records[1], 1, wire)
    destination = tmp_path / "backup"
    with pytest.raises(KernelError):
        await backup_product_state(root, destination)
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("field", ["sequence", "state"])
async def test_historical_sql_indices_remain_bound_to_domain_records(
    reference_state, tmp_path, field
):
    root, records = reference_state
    original = records[1]
    changed = original.model_copy(update={field: 3 if field == "sequence" else "interrupted"})
    digest = workspace_transaction_record_digest(changed)
    changed = changed.model_copy(update={"record_digest": digest})
    WorkspaceTransactionRecord.model_validate_json(changed.model_dump_json(), strict=True)
    wire = _wire(root, original, sequence=1)
    wire.update({field: getattr(changed, field), "record_digest": digest})
    _replace_event(root, original, 1, wire)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.decode_payload(_events(root, original.transaction_id)[1][2]).record == changed
    destination = tmp_path / "backup"
    with pytest.raises(KernelError) as error:
        await backup_product_state(root, destination)
    assert error.value.code == "product_backup_state_invalid"
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("same_identity", [False, True])
async def test_foreign_historical_plan_or_identity_is_rejected(
    reference_state, tmp_path, same_identity
):
    root, records = reference_state
    prepared = prepare_workspace_transaction(
        tmp_path / "workspace",
        {"x.txt": DesiredWorkspaceFile(b"different-plan\n", 0o644)},
        request_id=records[0].plan.request_id,
        transaction_id=records[0].transaction_id if same_identity else None,
        now=utc_now(),
    )
    foreign_root = tmp_path / "foreign-state"
    with SQLiteWorkspaceTransactionStore(foreign_root / "workspace-transactions") as store:
        foreign = store.save(prepared)
    wire = _wire(foreign_root, foreign)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        for blob in (foreign_root / "workspace-transactions/blobs").iterdir():
            store.put_blob(blob.name, blob.read_bytes())
    _replace_event(root, records[0], 0, wire)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        decoded = store.decode_payload(_events(root, records[0].transaction_id)[0][2])
        assert decoded.record == foreign
        assert store.load(records[-1].transaction_id) == records[-1]
    destination = tmp_path / "backup"
    with pytest.raises(KernelError) as error:
        await backup_product_state(root, destination)
    assert error.value.code == "product_backup_state_invalid"
    _assert_unpublished(root, destination)


async def test_schema_one_inline_bytes_backup_and_restore_without_migration(
    reference_state, tmp_path
):
    root, records = reference_state
    plan_path = "workspace-transactions/blobs/" + _wire(root, records[0])["plan_ref"]["sha256"]
    payloads = [json.dumps(json.loads(record.model_dump_json()), indent=2) for record in records]
    with sqlite3.connect(root / WORKSPACE_DATABASE) as database:
        database.execute("UPDATE delivery_metadata SET value='1' WHERE key='schema_version'")
        for record, payload in zip(records, payloads, strict=True):
            database.execute(
                "UPDATE workspace_transaction_events SET payload=? "
                "WHERE transaction_id=? AND sequence=?",
                (payload, str(record.transaction_id), record.sequence),
            )
        database.execute(
            "UPDATE workspace_transactions SET payload=? WHERE transaction_id=?",
            (payloads[-1], str(records[-1].transaction_id)),
        )
    (root / plan_path).unlink()
    original_bytes = (root / WORKSPACE_DATABASE).read_bytes()
    original_events = _events(root, records[0].transaction_id)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.load(records[-1].transaction_id) == records[-1]
        for record, payload in zip(records, payloads, strict=True):
            decoded = store.decode_payload(payload)
            assert decoded.record == record and decoded.references == ()
    destination = tmp_path / "backup"
    manifest = await backup_product_state(root, destination)
    assert (root / WORKSPACE_DATABASE).read_bytes() == original_bytes
    assert await verify_product_backup(root, destination) == manifest
    result = await restore_product_state(
        root, destination, restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    assert result.status == "restored"
    for state in (root, destination / "state"):
        assert _events(state, records[0].transaction_id) == original_events
        with closing(sqlite3.connect(state / WORKSPACE_DATABASE)) as database:
            assert database.execute(
                "SELECT value FROM delivery_metadata WHERE key='schema_version'"
            ).fetchone() == ("1",)
            assert database.execute("SELECT payload FROM workspace_transactions").fetchone() == (
                payloads[-1],
            )
        assert not (state / plan_path).exists()


async def test_mixed_inline_and_reference_history_preserves_original_bytes(
    reference_state, tmp_path
):
    root, records = reference_state
    payload = "\n" + json.dumps(json.loads(records[0].model_dump_json()), indent=2) + "\n"
    with sqlite3.connect(root / WORKSPACE_DATABASE) as database:
        database.execute(
            "UPDATE workspace_transaction_events SET payload=? "
            "WHERE transaction_id=? AND sequence=0",
            (payload, str(records[0].transaction_id)),
        )
    history = _events(root, records[0].transaction_id)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.decode_payload(payload).references == ()
        assert store.load(records[-1].transaction_id) == records[-1]
    destination = tmp_path / "backup"
    manifest = await backup_product_state(root, destination)
    assert await verify_product_backup(root, destination) == manifest
    assert _events(root, records[0].transaction_id) == history
    assert _events(destination / "state", records[0].transaction_id) == history


@pytest.mark.skipif(os.name != "posix", reason="长路径夹具使用POSIX相对句柄")
async def test_oversized_legacy_inline_record_is_not_repaired(complete_state, tmp_path):
    root, _ = complete_state
    prepared = _long_prepared(tmp_path / "long-workspace")
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        record = store.save(prepared)
    payload = record.model_dump_json()
    assert len(payload.encode()) > 512 * 1024
    with sqlite3.connect(root / WORKSPACE_DATABASE) as database:
        database.execute(
            "UPDATE workspace_transactions SET payload=? WHERE transaction_id=?",
            (payload, str(record.transaction_id)),
        )
        database.execute(
            "UPDATE workspace_transaction_events SET payload=? WHERE transaction_id=?",
            (payload, str(record.transaction_id)),
        )
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        with pytest.raises(KernelError):
            store.load(record.transaction_id)
    history = _events(root, record.transaction_id)
    destination = tmp_path / "backup"
    with pytest.raises(KernelError):
        await backup_product_state(root, destination)
    assert _events(root, record.transaction_id) == history
    _assert_unpublished(root, destination)


@pytest.mark.parametrize("version", ["0", "4", "999"])
async def test_workspace_unknown_schema_remains_rejected(complete_state, version):
    root, _ = complete_state
    with sqlite3.connect(root / WORKSPACE_DATABASE) as database:
        database.execute(
            "UPDATE delivery_metadata SET value=? WHERE key='schema_version'", (version,)
        )
    with pytest.raises(KernelError) as error:
        _schemas(root, False, MaintenanceIOControl())
    assert error.value.code == "product_backup_state_invalid"


@pytest.mark.parametrize(
    ("path", "table", "version"),
    [
        ("action-audit.db", "action_audit_metadata", "1"),
        ("execution-plans.db", "execution_store_metadata", "3"),
        ("product-config.db", "product_config_metadata", "2"),
        (PROCESS_DATABASE, "process_store_metadata", "1"),
    ],
)
async def test_other_store_schema_admission_is_not_widened(complete_state, path, table, version):
    root, _ = complete_state
    process = path == PROCESS_DATABASE
    if process:
        with SQLiteProcessLeaseStore(root / path):
            pass
    with sqlite3.connect(root / path) as database:
        database.execute(f"UPDATE {table} SET value=? WHERE key='schema_version'", (version,))
    with pytest.raises(KernelError) as error:
        _schemas(root, process, MaintenanceIOControl())
    assert error.value.code == "product_backup_state_invalid"
