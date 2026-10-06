"""事务新领域与物理 ReaderStore：真实文件、原 CAS、历史和控制负对照。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    WorkspaceTransactionPlan,
    WorkspaceTransactionRecord,
    new_transaction_record,
    transition_transaction_record,
    workspace_transaction_plan_fingerprint,
    workspace_transaction_record_digest,
)
from harnessix.delivery.planner import (
    DesiredWorkspaceFile,
    PreparedWorkspaceTransaction,
    prepare_workspace_transaction,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_record_codec import encode_workspace_record
from harnessix.delivery.workspace_record_contracts import MAX_STORED_RECORD_BYTES
from harnessix.delivery.workspace_v2_contracts import (
    WorkspaceTransactionPlanV2,
    WorkspaceTransactionRecordV2,
)
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _checkpoint():
    pass


def _prepared(
    root: Path,
    store,
    *,
    count=1,
    long=False,
    request_id="new-domain",
    before_body=b"before\n",
    after_body=b"after\n",
):
    """从真实叶文件及正式 Snapshot v2 构建完整 Plan，不使用未验证模型。"""
    root.mkdir(mode=0o700)
    paths, mutations, blobs = [], [], {}
    for index in range(count):
        components = [f"d{index:03}", "nested"]
        if long:
            components = [f"d{index:03}-" + "x" * 155] + ["n" * 160] * 4
        path = "/".join([*components, "leaf.txt"])
        target = root / path
        target.parent.mkdir(mode=0o700, parents=True)
        target.write_bytes(before_body)
        target.chmod(0o644)
        before, after = target.read_bytes(), after_body
        versions = []
        for body in (before, after):
            digest = hashlib.sha256(body).hexdigest()
            blobs[digest] = body
            versions.append(dict(presence="file", sha256=digest, size=len(body), mode=420))
        paths.append(WorkspaceResourceRequest(path=path, access="write"))
        mutations.append(dict(path=path, before=versions[0], after=versions[1]))
    snapshot = capture_workspace_snapshot_v2(
        root,
        resources=paths,
        checkpoint=_checkpoint,
        write_blob=store.put_blob,
        read_blob=store._read_blob,
    )
    payload = dict(
        spec_version="harnessix.workspace-transaction-plan/v2",
        transaction_id=str(uuid4()),
        request_id=request_id,
        source=snapshot.model_dump(mode="json"),
        mutations=mutations,
        created_at="2026-10-05T00:00:00Z",
    )
    payload["fingerprint"] = _digest(payload)
    plan = WorkspaceTransactionPlanV2.model_validate_json(_canonical(payload), strict=True)
    return PreparedWorkspaceTransaction(plan, blobs)


def _schema(store):
    return store._db.execute("SELECT value FROM delivery_metadata").fetchone()[0]


def _payload(store, record):
    return store._db.execute(
        "SELECT payload FROM workspace_transactions WHERE transaction_id=?",
        (str(record.transaction_id),),
    ).fetchone()[0]


def _closure(store, plan):
    reference = plan.source.parent_closure
    manifest = json.loads(store._read_blob(reference.sha256))
    return reference.sha256, [item["sha256"] for item in manifest["chunks"]]


@pytest.mark.parametrize("count,long", [(1, False), (128, False), (255, True)])
def test_complete_save_reopen_history_and_backup_references(tmp_path, count, long):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        prepared = _prepared(tmp_path / "workspace", store, count=count, long=long)
        assert _schema(store) == "2"
        record = store.save(prepared)
        assert type(record) is WorkspaceTransactionRecordV2
        assert _schema(store) == "3"
        assert workspace_transaction_plan_fingerprint(record.plan) == record.plan.fingerprint
        assert workspace_transaction_record_digest(record) == record.record_digest
        manifest, chunks = _closure(store, record.plan)
        publishing = transition_transaction_record(record, state="publishing", cursor=0, now=NOW)
        store.transition(record, publishing)
        interrupted = transition_transaction_record(
            publishing, state="interrupted", cursor=count // 2, now=NOW
        )
        store.transition(publishing, interrupted)
        assert store.save(prepared) == interrupted
        payloads = store._db.execute(
            "SELECT payload FROM workspace_transaction_events ORDER BY sequence"
        ).fetchall()
        for (payload,) in payloads:
            assert len(payload.encode()) <= MAX_STORED_RECORD_BYTES
            wire = json.loads(payload)
            assert wire["spec_version"] == "harnessix.workspace-stored-record/v3"
            decoded = store.decode_payload(payload)
            assert type(decoded.record) is WorkspaceTransactionRecordV2
            assert decoded.record.plan == prepared.plan
            assert [ref.sha256 for ref in decoded.references] == [
                wire["plan_ref"]["sha256"],
                manifest,
                *chunks,
            ]
            for reference in decoded.references:
                body = store._read_blob(reference.sha256)
                assert len(body) == reference.size
                assert hashlib.sha256(body).hexdigest() == reference.sha256
        if long:
            assert len(prepared.plan.model_dump_json().encode()) > MAX_STORED_RECORD_BYTES
        # 正式捕获端口同时保留 cwd；其余显式项只有叶访问。
        assert len(prepared.plan.source.resources) == count + 1
    original = (state / "transactions.db").read_bytes()
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        assert store.load(record.transaction_id) == interrupted
        assert _schema(store) == "3"
    assert (state / "transactions.db").read_bytes() == original


@pytest.mark.parametrize("kind", ["manifest", "chunk", "plan"])
def test_missing_metadata_never_falls_back_to_file_images(tmp_path, monkeypatch, kind):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        prepared = _prepared(tmp_path / "workspace", store)
        record = store.save(prepared)
        payload = _payload(store, record)
        manifest, chunks = _closure(store, record.plan)
        digest = dict(
            manifest=manifest, chunk=chunks[0], plan=json.loads(payload)["plan_ref"]["sha256"]
        )[kind]
        (state / "blobs" / digest).unlink()

        def forbidden_image(_digest):
            raise AssertionError("Reader不能通过文件镜像降级")

        monkeypatch.setattr(store, "blob", forbidden_image)
        with pytest.raises(KernelError, match="账本损坏") as error:
            store.load(record.transaction_id)
        assert error.value.code == "delivery_store_corrupt"
        with pytest.raises(KernelError) as error:
            store.decode_payload(payload)
        assert error.value.code == "delivery_store_corrupt"


@pytest.mark.parametrize("damage", ["version", "domain", "approvaldigest", "recorddigest", "size"])
def test_physical_binding_and_version_damage(tmp_path, damage):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        payload = json.loads(_payload(store, record))
        if damage == "version":
            payload["spec_version"] = "harnessix.workspace-stored-record/v4"
        elif damage == "domain":
            payload["domain_spec_version"] = "harnessix.workspace-transaction-record/v1"
        elif damage == "approvaldigest":
            payload["plan_ref"]["fingerprint"] = "f" * 64
        elif damage == "recorddigest":
            payload["record_digest"] = "f" * 64
        else:
            payload["plan_ref"]["size"] += 1
        with pytest.raises(KernelError) as error:
            store.decode_payload(_canonical(payload).decode())
        assert error.value.code == "delivery_store_corrupt"


def test_cross_root_closure_rejected_even_with_valid_new_hashes(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        first = _prepared(tmp_path / "first", store)
        second = _prepared(tmp_path / "second", store, request_id="other-root")
        payload = first.plan.model_dump(mode="json", exclude={"fingerprint"})
        source = payload["source"]
        source["parent_closure"] = second.plan.source.parent_closure.model_dump(mode="json")
        source["revision"] = _digest(
            {k: v for k, v in source.items() if k not in {"spec_version", "revision"}}
        )
        payload["fingerprint"] = _digest(payload)
        plan = WorkspaceTransactionPlanV2.model_validate_json(_canonical(payload), strict=True)
        with pytest.raises(KernelError) as error:
            store.save(PreparedWorkspaceTransaction(plan, first.blobs))
        assert error.value.code == "delivery_store_corrupt"
        assert _schema(store) == "2"
        assert store._db.execute("SELECT count(*) FROM workspace_transactions").fetchone() == (0,)


@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        ReadToolError("timeout"),
        ValueError("父控制"),
        KernelError("delivery_blob_corrupt", "父控制"),
    ],
)
def test_checkpoint_exceptions_survive_reader_and_sql_rollback(tmp_path, failure):
    armed = False

    def checkpoint():
        if armed:
            raise failure

    with SQLiteWorkspaceTransactionStore(tmp_path / "state", checkpoint=checkpoint) as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        armed = True
        with pytest.raises(type(failure)) as error:
            store.load(record.transaction_id)
        assert error.value is failure
        with pytest.raises(type(failure)) as error:
            store.decode_payload(_payload(store, record))
        assert error.value is failure


@pytest.mark.parametrize("operation", ["save", "transition"])
def test_upgrade_and_state_write_are_one_sql_transaction(tmp_path, monkeypatch, operation):
    armed = False
    failure = TurnCancelled()
    holder = {}

    def checkpoint():
        if armed and holder["store"]._db.in_transaction:
            assert _schema(holder["store"]) == "3"
            raise failure

    with SQLiteWorkspaceTransactionStore(tmp_path / "state", checkpoint=checkpoint) as store:
        holder["store"] = store
        prepared = _prepared(tmp_path / "workspace", store)
        if operation == "transition":
            record = store.save(prepared)
            store._db.execute("UPDATE delivery_metadata SET value='2'")
            updated = transition_transaction_record(record, state="publishing", cursor=0, now=NOW)
        before = store._db.execute("SELECT * FROM workspace_transactions").fetchall()
        history = store._db.execute("SELECT * FROM workspace_transaction_events").fetchall()
        armed = True
        with pytest.raises(TurnCancelled) as error:
            if operation == "save":
                store.save(prepared)
            else:
                store.transition(record, updated)
        assert error.value is failure
        assert _schema(store) == "2"
        assert store._db.execute("SELECT * FROM workspace_transactions").fetchall() == before
        assert store._db.execute("SELECT * FROM workspace_transaction_events").fetchall() == history


@pytest.mark.parametrize("confirmation", ["plan", "manifest", "chunk"])
@pytest.mark.parametrize("operation", ["save", "transition"])
def test_every_closure_blob_is_durably_confirmed_before_commit(
    tmp_path, monkeypatch, confirmation, operation
):
    from harnessix.delivery import store as implementation

    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        prepared = _prepared(tmp_path / "workspace", store)
        manifest, chunks = _closure(store, prepared.plan)
        plan_digest = hashlib.sha256(
            prepared.plan.model_dump_json(warnings="error").encode()
        ).hexdigest()
        if operation == "transition":
            record = store.save(prepared)
            updated = transition_transaction_record(record, state="publishing", cursor=0, now=NOW)
        before = store._db.execute("SELECT * FROM workspace_transactions").fetchall()
        history = store._db.execute("SELECT * FROM workspace_transaction_events").fetchall()
        schema = _schema(store)
        rejected = dict(plan=plan_digest, manifest=manifest, chunk=chunks[0])[confirmation]
        confirm = implementation.confirm_blob_durable

        def fail_confirmation(path, *args):
            if path.name == rejected:
                raise KernelError("delivery_storage_unavailable", "耐久确认丢失")
            return confirm(path, *args)

        monkeypatch.setattr(implementation, "confirm_blob_durable", fail_confirmation)
        with pytest.raises(KernelError) as error:
            if operation == "save":
                store.save(prepared)
            else:
                store.transition(record, updated)
        assert error.value.code == "delivery_storage_unavailable"
        assert _schema(store) == schema
        assert store._db.execute("SELECT * FROM workspace_transactions").fetchall() == before
        assert store._db.execute("SELECT * FROM workspace_transaction_events").fetchall() == history


def test_old_pretty_bytes_schema2_and_raw_cas_compare_are_preserved(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "target.txt").write_bytes(b"before\n")
    (root / "target.txt").chmod(0o644)
    old = prepare_workspace_transaction(
        root, {"target.txt": DesiredWorkspaceFile(b"after\n", 420)}, request_id="old", now=NOW
    )
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(old)
        assert type(record) is WorkspaceTransactionRecord
        assert _schema(store) == "2"
        assert (
            json.loads(_payload(store, record))["spec_version"]
            == "harnessix.workspace-stored-record/v2"
        )
        original = record.model_dump_json(indent=2)
        store._db.execute("UPDATE workspace_transactions SET payload=?", (original,))
        store._db.execute("UPDATE workspace_transaction_events SET payload=?", (original,))
        new = _prepared(tmp_path / "new-workspace", store)
        store.save(new)
        assert _schema(store) == "3"
        assert store.load(record.transaction_id) == record
        assert _payload(store, record) == original
        updated = transition_transaction_record(record, state="publishing", cursor=0, now=NOW)
        store.transition(record, updated)
        assert _schema(store) == "3"
        assert (
            json.loads(_payload(store, record))["spec_version"]
            == "harnessix.workspace-stored-record/v2"
        )
        assert store._db.execute(
            "SELECT payload FROM workspace_transaction_events "
            "WHERE transaction_id=? AND sequence=0",
            (str(record.transaction_id),),
        ).fetchone() == (original,)
        with pytest.raises(KernelError) as error:
            store.transition(record, updated)
        assert error.value.code == "delivery_transaction_stale"


def test_new_models_inherit_full_validation_without_accepting_old_generations(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        plan = _prepared(tmp_path / "workspace", store).plan
        assert isinstance(plan, WorkspaceTransactionPlan)
        record = new_transaction_record(plan)
        assert isinstance(record, WorkspaceTransactionRecord)
        assert record.plan.source.parent_closure == plan.source.parent_closure
        with pytest.raises(ValidationError):
            WorkspaceTransactionPlan.model_validate_json(plan.model_dump_json(), strict=True)
        with pytest.raises(ValidationError):
            WorkspaceTransactionRecord.model_validate_json(record.model_dump_json(), strict=True)
        with pytest.raises(ValidationError):
            transition_transaction_record(record, state="published", cursor=0, now=NOW)


def test_readonly_priority_and_schema_admission_without_upgrade(tmp_path):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        prepared = _prepared(tmp_path / "workspace", store)
        assert _schema(store) == "2"
    for version in ("1", "2", "3"):
        with sqlite3.connect(state / "transactions.db") as db:
            db.execute("UPDATE delivery_metadata SET value=?", (version,))
        with SQLiteWorkspaceTransactionStore(
            state, read_only=True, checkpoint=lambda: (_ for _ in ()).throw(TurnCancelled())
        ) as store:
            assert _schema(store) == version
            with pytest.raises(KernelError) as error:
                store.save(prepared)
            assert error.value.code == "delivery_store_read_only"


def _rebind_plan(plan, source):
    payload = plan.model_dump(mode="json", exclude={"fingerprint"})
    source["revision"] = _digest(
        {key: value for key, value in source.items() if key not in {"spec_version", "revision"}}
    )
    payload["source"] = source
    payload["fingerprint"] = _digest(payload)
    return WorkspaceTransactionPlanV2.model_validate_json(_canonical(payload), strict=True)


def test_all_rechunked_real_parent_references_are_complete(tmp_path):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        prepared = _prepared(tmp_path / "workspace", store, count=3)
        manifest_digest, chunks = _closure(store, prepared.plan)
        manifest = json.loads(store._read_blob(manifest_digest))
        original = json.loads(store._read_blob(chunks[0]))
        manifest["chunks"] = []
        for start, entries in ((0, original["entries"][:3]), (3, original["entries"][3:])):
            body = _canonical({**original, "start_index": start, "entries": entries})
            digest = hashlib.sha256(body).hexdigest()
            store.put_blob(digest, body)
            manifest["chunks"].append(
                dict(sha256=digest, size=len(body), start_index=start, count=len(entries))
            )
        body = _canonical(manifest)
        digest = hashlib.sha256(body).hexdigest()
        store.put_blob(digest, body)
        source = prepared.plan.source.model_dump(mode="json")
        source["parent_closure"].update(sha256=digest, size=len(body))
        plan = _rebind_plan(prepared.plan, source)
        record = store.save(PreparedWorkspaceTransaction(plan, prepared.blobs))
        payload = _payload(store, record)
        references = store.decode_payload(payload).references
        assert [ref.sha256 for ref in references[2:]] == [
            item["sha256"] for item in manifest["chunks"]
        ]
        assert len(references) == 4
        (state / "blobs" / references[-1].sha256).unlink()
        with pytest.raises(KernelError) as error:
            store.decode_payload(payload)
        assert error.value.code == "delivery_store_corrupt"


@pytest.mark.parametrize(
    "failure",
    [TurnCancelled(), asyncio.CancelledError(), ReadToolError("timeout"), OSError("父期限")],
)
@pytest.mark.parametrize("phase", ["plan", "manifest", "chunk"])
def test_control_after_actual_blob_read_is_not_classified_as_corrupt(
    tmp_path, monkeypatch, failure, phase
):
    armed = False

    def checkpoint():
        if armed:
            raise failure

    with SQLiteWorkspaceTransactionStore(tmp_path / "state", checkpoint=checkpoint) as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        payload = _payload(store, record)
        manifest, chunks = _closure(store, record.plan)
        selected = dict(
            plan=json.loads(payload)["plan_ref"]["sha256"], manifest=manifest, chunk=chunks[0]
        )[phase]
        read = store._read_blob

        def interrupted_read(digest):
            nonlocal armed
            body = read(digest)
            if digest == selected:
                armed = True
            return body

        monkeypatch.setattr(store, "_read_blob", interrupted_read)
        with pytest.raises(type(failure)) as error:
            store.load(record.transaction_id)
        assert error.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["turn", "parent"])
async def test_real_async_control_interrupts_same_callback_during_cas_read(
    tmp_path, monkeypatch, kind
):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        selected = record.plan.source.parent_closure.sha256
    token = CancelToken()
    reached = False

    async def read_record():
        with SQLiteWorkspaceTransactionStore(
            state, read_only=True, checkpoint=parent_cancel_checkpointer(token.checkpoint)
        ) as store:
            read = store._read_blob

            def interrupted_read(digest):
                nonlocal reached
                body = read(digest)
                if digest == selected:
                    reached = True
                    if kind == "turn":
                        token.cancel()
                    else:
                        asyncio.current_task().cancel()
                return body

            monkeypatch.setattr(store, "_read_blob", interrupted_read)
            store.load(record.transaction_id)

    task = asyncio.create_task(read_record())
    with pytest.raises(TurnCancelled if kind == "turn" else asyncio.CancelledError):
        await task
    assert reached
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        assert store.load(record.transaction_id) == record


def test_new_reader_utf8_bound_missing_port_and_no_store_lifetime_timer(tmp_path, monkeypatch):
    from harnessix.tools.workspace import ReadOperation

    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        payload = _payload(store, record)
        padded = payload + " " * (MAX_STORED_RECORD_BYTES - len(payload.encode()))
        assert store.decode_payload(padded).record == record
        with pytest.raises(KernelError) as error:
            store.decode_payload(padded + " ")
        assert error.value.code == "delivery_store_corrupt"
        writes = []
        with pytest.raises(KernelError) as error:
            encode_workspace_record(record, lambda digest, body: writes.append(digest))
        assert error.value.code == "delivery_record_invalid"
        assert writes == []

    def forbidden_timer(*_args, **_kwargs):
        raise AssertionError("Store不能创建本次操作之外的读取期限")

    monkeypatch.setattr(ReadOperation, "__init__", forbidden_timer)
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        assert store.load(record.transaction_id) == record


@pytest.mark.parametrize(
    "code", ["delivery_storage_unavailable", "execution_plan_stale", "workspace_closure_corrupt"]
)
@pytest.mark.parametrize("phase", ["plan", "manifest", "chunk"])
def test_non_corruption_port_error_keeps_original_identity(tmp_path, monkeypatch, code, phase):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        failure = KernelError(code, "宿主端口失败")
        manifest, chunks = _closure(store, record.plan)
        selected = dict(
            plan=json.loads(_payload(store, record))["plan_ref"]["sha256"],
            manifest=manifest,
            chunk=chunks[0],
        )[phase]
        read = store._read_blob

        def unavailable(digest):
            if digest == selected:
                raise failure
            return read(digest)

        monkeypatch.setattr(store, "_read_blob", unavailable)
        with pytest.raises(KernelError) as error:
            store.load(record.transaction_id)
        assert error.value is failure


def test_actual_image_budget_and_original_cas_blob_limit(tmp_path):
    size = 8 * 1024 * 1024
    before, after = b"a" * size, b"b" * size
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        prepared = _prepared(
            tmp_path / "workspace",
            store,
            count=2,
            before_body=before,
            after_body=after,
        )
        assert (
            sum(m.before.size + m.after.size for m in prepared.plan.mutations) == 32 * 1024 * 1024
        )
        record = store.save(prepared)
        assert store.load(record.transaction_id) == record
        for digest, body in prepared.blobs.items():
            assert store.blob(digest) == body
        with pytest.raises(ValidationError, match="容量无效"):
            _prepared(
                tmp_path / "too-large-images",
                store,
                count=3,
                request_id="too-large",
                before_body=before,
                after_body=after,
            )
        over = before + b"x"
        with pytest.raises(KernelError) as error:
            store.put_blob(hashlib.sha256(over).hexdigest(), over)
        assert error.value.code == "delivery_blob_invalid"


def test_transition_upgrades_only_metadata_and_does_not_rewrite_old_event(tmp_path):
    state = tmp_path / "state"
    with SQLiteWorkspaceTransactionStore(state) as store:
        record = store.save(_prepared(tmp_path / "workspace", store))
        original = _payload(store, record)
        store._db.execute("UPDATE delivery_metadata SET value='2'")
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        assert store.load(record.transaction_id) == record
        assert _schema(store) == "2"
    with SQLiteWorkspaceTransactionStore(state) as store:
        assert store.load(record.transaction_id) == record
        assert _schema(store) == "2"
        updated = transition_transaction_record(record, state="publishing", cursor=0, now=NOW)
        store.transition(record, updated)
        assert _schema(store) == "3"
        assert store._db.execute(
            "SELECT payload FROM workspace_transaction_events WHERE sequence=0"
        ).fetchone() == (original,)
        assert json.loads(_payload(store, updated))["plan_ref"] == json.loads(original)["plan_ref"]
