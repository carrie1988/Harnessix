"""父闭包备份只读消费：准确代际准入、完整引用清单和原历史验真。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import transition_transaction_record
from harnessix.delivery.planner import PreparedWorkspaceTransaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionPlanV2
from harnessix.domain.models import (
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    PolicyDecisionKind,
    RiskLevel,
    utc_now,
)
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionIntent,
    ExecutionPolicyBinding,
    SandboxBindingV2,
)
from harnessix.execution.planner import build_capability_evidence_v2, build_execution_plan_v3
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.product_config import state_backup_records as records
from harnessix.product_config import state_backup_validation as validation
from harnessix.product_config.state_backup_contracts import (
    PROCESS_DATABASE,
    ephemeral_state_file,
    state_directory_allowed,
)
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.session.maintenance_io import MaintenanceIOControl
from harnessix.trusted_actions.contracts import build_trusted_tool_binding
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from tests.product_config.test_product_state_backup import complete_state as _complete_state

complete_state = _complete_state

_METADATA = {
    "action-audit.db": ("action_audit_metadata", "2"),
    "execution-plans.db": ("execution_store_metadata", "1"),
    "product-config.db": ("product_config_metadata", "1"),
    "workspace-transactions/transactions.db": ("delivery_metadata", "2"),
    PROCESS_DATABASE: ("process_store_metadata", "2"),
}


@pytest.fixture
def schema_root(tmp_path: Path, monkeypatch) -> Path:
    # Session迁移仍走原校验；此夹具仅独立覆盖本次变更的元数据白名单。
    monkeypatch.setattr(validation, "DATABASES", tuple(_METADATA)[:-1])
    for path, (table, version) in _METADATA.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target) as database:
            database.execute(f"CREATE TABLE {table} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            database.execute(f"INSERT INTO {table} VALUES ('schema_version', ?)", (version,))
    return tmp_path


def _schema(root: Path, path: str, version: str) -> None:
    table = _METADATA[path][0]
    with sqlite3.connect(root / path) as database:
        database.execute(f"UPDATE {table} SET value=? WHERE key='schema_version'", (version,))


@pytest.mark.parametrize(
    ("path", "version"),
    [
        ("action-audit.db", "2"),
        ("action-audit.db", "3"),
        ("execution-plans.db", "1"),
        ("execution-plans.db", "2"),
        ("workspace-transactions/transactions.db", "1"),
        ("workspace-transactions/transactions.db", "2"),
        ("workspace-transactions/transactions.db", "3"),
    ],
)
def test_exact_schema_admission_is_readonly(schema_root: Path, path: str, version: str) -> None:
    _schema(schema_root, path, version)
    before = {name: (schema_root / name).read_bytes() for name in _METADATA}
    validation._schemas(schema_root, True, MaintenanceIOControl())
    assert before == {name: (schema_root / name).read_bytes() for name in _METADATA}


@pytest.mark.parametrize(
    ("path", "version"),
    [
        ("action-audit.db", "1"),
        ("action-audit.db", "4"),
        ("execution-plans.db", "0"),
        ("execution-plans.db", "3"),
        ("workspace-transactions/transactions.db", "0"),
        ("workspace-transactions/transactions.db", "4"),
        ("product-config.db", "2"),
        (PROCESS_DATABASE, "1"),
        (PROCESS_DATABASE, "3"),
    ],
)
def test_schema_admission_keeps_other_constraints(
    schema_root: Path, path: str, version: str
) -> None:
    _schema(schema_root, path, version)
    with pytest.raises(KernelError) as error:
        validation._schemas(schema_root, True, MaintenanceIOControl())
    assert error.value.code == "product_backup_state_invalid"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _snapshot(tag: str = "original") -> tuple[WorkspaceSnapshotV2, dict[str, bytes]]:
    """独立组装两块父历史，所有事实为纯合同夹具，不调用原生捕获。"""
    scope = dict(
        platform="posix",
        root_identity=_sha(tag.encode()),
        root_path_digest=_sha(b"backup-workspace"),
    )
    scope.update(workspace_id=_sha(_canonical(scope)), cwd=".", external_roots=[])
    parents = [
        dict(
            location="workspace",
            path=path,
            access="read",
            kind="directory",
            identity=_sha((tag + path).encode()),
            content_sha256=None,
            size=1,
        )
        for path in (".", "a", "b")
    ]
    resources = [
        parents[0],
        *[
            dict(
                location="workspace",
                path=path,
                access="write",
                kind="missing",
                identity=_sha((tag + path).encode()),
                content_sha256=None,
                size=0,
            )
            for path in ("a/one.txt", "b/two.txt")
        ],
    ]
    target_digest = _sha(
        _canonical(
            [{key: item[key] for key in ("location", "path", "access")} for item in resources]
        )
    )
    blobs = {}
    chunks = []
    for start, entries in ((0, parents[:2]), (2, parents[2:])):
        body = _canonical(
            dict(
                spec_version="harnessix.workspace-parent-observations/v1",
                start_index=start,
                entries=[
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"location", "path", "access"}
                    }
                    for item in entries
                ],
            )
        )
        digest = _sha(body)
        blobs[digest] = body
        chunks.append(dict(sha256=digest, size=len(body), start_index=start, count=len(entries)))
    manifest = _canonical(
        dict(
            **scope,
            spec_version="harnessix.workspace-parent-closure/v1",
            target_set_digest=target_digest,
            nodes=[
                dict(parent=None, name=".", location="workspace"),
                dict(parent=0, name="a", location=None),
                dict(parent=0, name="b", location=None),
            ],
            chunks=chunks,
            observations_digest=_sha(_canonical(parents)),
        )
    )
    digest = _sha(manifest)
    blobs[digest] = manifest
    payload = dict(
        **scope,
        resources=resources,
        parent_closure=dict(
            sha256=digest,
            size=len(manifest),
            parent_count=3,
            target_set_digest=target_digest,
        ),
        algorithm="selected-resources-parent-closure-sha256/v2",
    )
    payload.update(
        revision=_sha(_canonical(payload)), spec_version="harnessix.workspace-snapshot/v2"
    )
    return WorkspaceSnapshotV2.model_validate_json(_canonical(payload), strict=True), blobs


def _workspace_plan(
    snapshot: WorkspaceSnapshotV2, *, transaction_id: UUID | None = None
) -> WorkspaceTransactionPlanV2:
    payload = dict(
        spec_version="harnessix.workspace-transaction-plan/v2",
        transaction_id=str(transaction_id or uuid4()),
        request_id="backup-parent-closure",
        source=snapshot.model_dump(mode="json"),
        mutations=[
            dict(
                path="a/one.txt",
                before=dict(presence="absent", sha256=None, size=0, mode=None),
                after=dict(presence="file", sha256=_sha(b"after"), size=5, mode=0o644),
            )
        ],
        created_at=utc_now().isoformat().replace("+00:00", "Z"),
    )
    payload["fingerprint"] = _sha(_canonical(payload))
    return WorkspaceTransactionPlanV2.model_validate_json(_canonical(payload), strict=True)


@pytest.fixture
def closure_state(tmp_path: Path) -> tuple[Path, WorkspaceSnapshotV2, tuple[str, ...], UUID]:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    snapshot, blobs = _snapshot()
    plan = _workspace_plan(snapshot)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        for digest, body in blobs.items():
            store.put_blob(digest, body)
        record = store.save(PreparedWorkspaceTransaction(plan, {_sha(b"after"): b"after"}))
        for state in ("publishing", "published"):
            updated = transition_transaction_record(
                record,
                state=state,
                cursor=1 if state == "published" else 0,
                now=utc_now(),
            )
            store.transition(record, updated)
            record = updated
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        payload = store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
        references = store.decode_payload(payload).references
        assert len(references) == 4
        assert set(blobs) < {reference.sha256 for reference in references}
    return root, snapshot, tuple(reference.sha256 for reference in references), plan.transaction_id


def _tree_paths(
    root: Path, control: MaintenanceIOControl
) -> Iterator[tuple[PrivateStateTree, tuple[str, ...]]]:
    with PrivateStateTree(root) as tree:
        yield (
            tree,
            tuple(
                path
                for path in tree.files(
                    control, directories=state_directory_allowed, transient=ephemeral_state_file
                )
                if not ephemeral_state_file(path)
            ),
        )


def test_workspace_current_and_all_history_references_are_inventory_closed(closure_state) -> None:
    root, _snapshot_value, _digests, _identity = closure_state
    before = (root / "workspace-transactions/transactions.db").read_bytes()
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        records._delivery(tree, paths, control)
    assert (root / "workspace-transactions/transactions.db").read_bytes() == before


@pytest.mark.parametrize("reference_index", range(4))
def test_workspace_plan_manifest_and_every_chunk_must_be_in_inventory(
    closure_state, reference_index: int
) -> None:
    root, _snapshot_value, digests, _identity = closure_state
    omitted = "workspace-transactions/blobs/" + digests[reference_index]
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        assert omitted in paths
        with pytest.raises(KernelError) as error:
            records._delivery(tree, tuple(path for path in paths if path != omitted), control)
        assert error.value.code == "product_backup_state_invalid"


@pytest.mark.parametrize("reference_index", range(4))
@pytest.mark.parametrize("damage", ["missing", "sha", "size"])
def test_workspace_references_reject_missing_or_changed_original_bytes(
    closure_state, reference_index: int, damage: str
) -> None:
    root, _snapshot_value, digests, _identity = closure_state
    target = root / "workspace-transactions/blobs" / digests[reference_index]
    if damage == "missing":
        target.unlink()
    else:
        body = target.read_bytes()
        target.write_bytes(body + b" " if damage == "size" else b"x" + body[1:])
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with pytest.raises(KernelError):
            records._delivery(tree, paths, control)


@pytest.mark.parametrize("reference_index", range(3))
def test_history_only_closure_damage_is_not_hidden_by_valid_current_record(
    closure_state, reference_index: int
) -> None:
    from harnessix.delivery.workspace_record_codec import encode_workspace_record
    from harnessix.delivery.workspace_v2_contracts import new_transaction_record_v2

    root, _snapshot_value, _digests, identity = closure_state
    historic_snapshot, blobs = _snapshot("historical-root")
    historical = new_transaction_record_v2(
        _workspace_plan(historic_snapshot, transaction_id=identity)
    )
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        for digest, body in blobs.items():
            store.put_blob(digest, body)
        payload = encode_workspace_record(historical, store.put_blob, read_blob=store.blob)
        store._db.execute(
            "UPDATE workspace_transaction_events SET payload=? WHERE sequence=0", (payload,)
        )
    (root / "workspace-transactions/blobs" / tuple(blobs)[reference_index]).unlink()
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions", read_only=True) as store:
        assert store.load(identity).state == "published"
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with pytest.raises(KernelError) as error:
            records._delivery(tree, paths, control)
        assert error.value.code in {"delivery_store_corrupt", "delivery_blob_corrupt"}


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_workspace_reader_keeps_parent_maintenance_control(closure_state, reason: str) -> None:
    root, _snapshot_value, _digests, identity = closure_state
    control = MaintenanceIOControl(timeout=-1 if reason == "timeout" else 30)
    if reason == "cancel":
        control.cancel()
    with SQLiteWorkspaceTransactionStore(
        root / "workspace-transactions",
        read_only=True,
        checkpoint=control.checkpoint,
    ) as store:
        with pytest.raises(KernelError) as error:
            store.load(identity)
    assert error.value.code == (
        "maintenance_io_timeout" if reason == "timeout" else "maintenance_io_cancelled"
    )


def test_inventory_reader_validates_independent_closure_without_a_workspace_record(
    tmp_path: Path,
) -> None:
    snapshot, blobs = _snapshot()
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as store:
        for digest, body in blobs.items():
            store.put_blob(digest, body)
        assert store._db.execute("SELECT COUNT(*) FROM workspace_transactions").fetchone() == (0,)
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with SQLiteWorkspaceTransactionStore(
            root / "workspace-transactions", read_only=True, checkpoint=control.checkpoint
        ) as store:
            reader = records._inventory_reader(store, tree, paths, control)
            parents = read_workspace_parent_closure(snapshot, reader, checkpoint=control.checkpoint)
            assert [parent.path for parent in parents] == [".", "a", "b"]


@pytest.mark.parametrize("reference_index", range(3))
def test_independent_inventory_reader_checks_manifest_and_each_requested_chunk(
    closure_state, reference_index: int
) -> None:
    root, snapshot, digests, _identity = closure_state
    omitted = "workspace-transactions/blobs/" + digests[reference_index + 1]
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with SQLiteWorkspaceTransactionStore(
            root / "workspace-transactions", read_only=True, checkpoint=control.checkpoint
        ) as store:
            reader = records._inventory_reader(
                store, tree, tuple(path for path in paths if path != omitted), control
            )
            with pytest.raises(KernelError) as error:
                read_workspace_parent_closure(snapshot, reader, checkpoint=control.checkpoint)
            assert error.value.code == "product_backup_state_invalid"


def test_inventory_reader_detects_body_drift_after_cas_read(closure_state, monkeypatch) -> None:
    root, snapshot, _digests, _identity = closure_state
    original = SQLiteWorkspaceTransactionStore.blob

    def changed(store, digest):
        body = original(store, digest)
        (root / "workspace-transactions/blobs" / digest).write_bytes(body + b" ")
        return body

    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, "blob", changed)
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with SQLiteWorkspaceTransactionStore(
            root / "workspace-transactions", read_only=True, checkpoint=control.checkpoint
        ) as store:
            reader = records._inventory_reader(store, tree, paths, control)
            with pytest.raises(KernelError) as error:
                read_workspace_parent_closure(snapshot, reader, checkpoint=control.checkpoint)
            assert error.value.code == "product_backup_state_invalid"


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_inventory_reader_propagates_control_during_last_chunk(
    closure_state, monkeypatch, reason: str
) -> None:
    root, snapshot, digests, _identity = closure_state
    original = SQLiteWorkspaceTransactionStore.blob
    failure = KernelError(
        "maintenance_io_cancelled" if reason == "cancel" else "maintenance_io_timeout", "受控停止"
    )
    stopped = False
    control = MaintenanceIOControl()

    def checkpoint():
        if stopped:
            raise failure

    def stop(store, digest):
        nonlocal stopped
        body = original(store, digest)
        if digest == digests[-1]:
            stopped = True
        return body

    monkeypatch.setattr(control, "checkpoint", checkpoint)
    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, "blob", stop)
    for tree, paths in _tree_paths(root, control):
        with SQLiteWorkspaceTransactionStore(
            root / "workspace-transactions", read_only=True, checkpoint=control.checkpoint
        ) as store:
            reader = records._inventory_reader(store, tree, paths, control)
            with pytest.raises(KernelError) as error:
                read_workspace_parent_closure(snapshot, reader, checkpoint=control.checkpoint)
            assert error.value is failure


def _execution(snapshot: WorkspaceSnapshotV2) -> ExecutionPlanV3:
    capabilities = build_capability_evidence_v2(
        platform=snapshot.platform,
        provider="backup-fixture",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest="1" * 64,
    )
    return build_execution_plan_v3(
        ExecutionIntent(
            source="custom",
            source_id="backup-fixture",
            tool="fixture.read",
            tool_version="1",
            tool_fingerprint="2" * 64,
            effect_class=EffectClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
        ),
        snapshot,
        environment={},
        secrets=(),
        capabilities=capabilities,
        sandbox=SandboxBindingV2(
            level="host_guarded",
            backend="host",
            backend_version="1",
            network="none",
            capability_digest=capabilities.evidence_digest,
            profile_digest="3" * 64,
        ),
        policy=ExecutionPolicyBinding(
            version="backup-fixture/v1",
            decision=PolicyDecisionKind.REQUIRE_APPROVAL,
            policy_id="fixture-approval",
            reason_code="fixture",
        ),
    )


def _route(plan: ExecutionPlanV3) -> ActionRoutePlanV2:
    intent = plan.intent
    binding = build_trusted_tool_binding(
        source=intent.source,
        source_id=intent.source_id,
        tool=intent.tool,
        tool_version=intent.tool_version,
        tool_fingerprint=intent.tool_fingerprint,
        input_schema_sha256="4" * 64,
        effect_class=intent.effect_class,
        risk_level=intent.risk_level,
        recovery_mode="none",
        executor_id="backup-fixture",
    )
    payload = dict(
        spec_version="harnessix.action-route-plan/v2",
        invocation=dict(
            spec_version="harnessix.coding-action-invocation/v1",
            invocation_id=str(plan.plan_id),
            source=intent.source,
            source_id=intent.source_id,
            tool=intent.tool,
            tool_version=intent.tool_version,
            tool_fingerprint=intent.tool_fingerprint,
            arguments=intent.arguments,
            idempotency_key=intent.idempotency_key,
        ),
        binding=binding.model_dump(mode="json"),
        resources=[],
        resources_sha256=_sha(_canonical([])),
        execution=plan.model_dump(mode="json"),
        external_action_id=None,
    )
    payload["fingerprint"] = _sha(_canonical(payload))
    return ActionRoutePlanV2.model_validate_json(_canonical(payload), strict=True)


def _approval(plan: ExecutionPlanV3, fingerprint: str | None = None) -> ExecutionApprovalCheckpoint:
    digest = fingerprint or plan.fingerprint
    return ExecutionApprovalCheckpoint(
        plan_id=plan.plan_id,
        plan_fingerprint=digest,
        decision=ApprovalRecord(
            outcome=ApprovalOutcome.APPROVED, actor="backup-fixture", request_fingerprint=digest
        ),
    )


def _consumer_state(root: Path, *, route: bool) -> tuple[ExecutionPlanV3, tuple[str, ...]]:
    root.mkdir(mode=0o700)
    snapshot, blobs = _snapshot()
    plan = _execution(snapshot)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as workspace:
        for digest, body in blobs.items():
            workspace.put_blob(digest, body)
        with SQLiteExecutionPlanStore(
            root / "execution-plans.db", read_blob=workspace.blob
        ) as store:
            store.save_plan(plan)
            store.record_approval(_approval(plan))
        with SQLiteActionAuditStore(root / "action-audit.db", read_blob=workspace.blob) as store:
            if route:
                store.save_plan(_route(plan), initial_state="pending_approval")
        assert workspace._db.execute("SELECT COUNT(*) FROM workspace_transactions").fetchone() == (
            0,
        )
    return plan, tuple(blobs)


@pytest.mark.parametrize("has_route", [False, True])
def test_independent_execution_and_route_readers_do_not_need_workspace_records(
    tmp_path: Path, has_route: bool
) -> None:
    root = tmp_path / "state"
    plan, _digests = _consumer_state(root, route=has_route)
    before = {
        path: (root / path).read_bytes() for path in ("execution-plans.db", "action-audit.db")
    }
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        records._plans_and_routes(tree, (), paths, control)
    assert before == {path: (root / path).read_bytes() for path in before}
    with SQLiteWorkspaceTransactionStore(
        root / "workspace-transactions", read_only=True
    ) as workspace:
        with SQLiteExecutionPlanStore(
            root / "execution-plans.db", read_only=True, read_blob=workspace.blob
        ) as store:
            assert store.load_plan(plan.plan_id) == plan


@pytest.mark.parametrize("reference_index", range(3))
def test_independent_execution_inventory_closure_is_not_skipped(
    tmp_path: Path, reference_index: int
) -> None:
    root = tmp_path / "state"
    _plan, digests = _consumer_state(root, route=False)
    omitted = "workspace-transactions/blobs/" + digests[reference_index]
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with pytest.raises(KernelError) as error:
            records._plans_and_routes(
                tree, (), tuple(path for path in paths if path != omitted), control
            )
        assert error.value.code == "product_backup_state_invalid"


@pytest.mark.parametrize("reference_index", range(3))
def test_independent_route_reader_checks_all_inventory_references(
    tmp_path: Path, reference_index: int
) -> None:
    root = tmp_path / "state"
    _plan, digests = _consumer_state(root, route=True)
    omitted = "workspace-transactions/blobs/" + digests[reference_index]
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with SQLiteWorkspaceTransactionStore(
            root / "workspace-transactions", read_only=True, checkpoint=control.checkpoint
        ) as workspace:
            reader = records._inventory_reader(
                workspace, tree, tuple(path for path in paths if path != omitted), control
            )
            with SQLiteActionAuditStore(
                root / "action-audit.db",
                read_only=True,
                read_blob=reader,
                checkpoint=control.checkpoint,
            ) as store:
                with pytest.raises(KernelError) as error:
                    store.routes()
                assert error.value.code == "product_backup_state_invalid"


@pytest.mark.parametrize("reader", ["execution", "route"])
@pytest.mark.parametrize("reference_index", range(3))
@pytest.mark.parametrize("damage", ["missing", "sha", "size"])
def test_independent_reader_rejects_missing_and_corrupt_parent_history(
    tmp_path: Path, reader: str, reference_index: int, damage: str
) -> None:
    root = tmp_path / "state"
    plan, digests = _consumer_state(root, route=reader == "route")
    target = root / "workspace-transactions/blobs" / digests[reference_index]
    if damage == "missing":
        target.unlink()
    else:
        body = target.read_bytes()
        target.write_bytes(body + b" " if damage == "size" else b"x" + body[1:])
    with SQLiteWorkspaceTransactionStore(
        root / "workspace-transactions", read_only=True
    ) as workspace:
        if reader == "execution":
            with SQLiteExecutionPlanStore(
                root / "execution-plans.db", read_only=True, read_blob=workspace.blob
            ) as store:
                with pytest.raises(KernelError):
                    store.load_plan(plan.plan_id)
        else:
            with SQLiteActionAuditStore(
                root / "action-audit.db", read_only=True, read_blob=workspace.blob
            ) as store:
                with pytest.raises(KernelError):
                    store.routes()


def _failed_process(root: Path, plan: ExecutionPlanV3) -> None:
    # 仅Reader跨Store夹具，不启动Supervisor，也不代表Process生产路径支持Exec3。
    lease = ProcessLease(
        process_id=uuid4(),
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        process_spec_digest="5" * 64,
        capability_digest="6" * 64,
        launch_binding_digest="7" * 64,
        lifecycle="foreground",
        state="prepared",
        sequence=0,
        owner_token="8" * 64,
        deadline=utc_now(),
    )
    with SQLiteProcessLeaseStore(root / PROCESS_DATABASE) as store:
        store.create(lease)
        failed = lease.model_copy(
            update=dict(
                state="failed", sequence=1, finished_at=utc_now(), stop_reason="launch_failed"
            )
        )
        store.transition(lease, failed)


@pytest.mark.parametrize("reference_index", [None, 0, 1, 2])
def test_process_cross_store_readonly_reader_uses_same_inventory(
    tmp_path: Path, reference_index: int | None
) -> None:
    root = tmp_path / "state"
    plan, digests = _consumer_state(root, route=False)
    _failed_process(root, plan)
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        if reference_index is None:
            records._processes(tree, paths, control)
        else:
            omitted = "workspace-transactions/blobs/" + digests[reference_index]
            with pytest.raises(KernelError) as error:
                records._processes(tree, tuple(path for path in paths if path != omitted), control)
            assert error.value.code == "product_backup_state_invalid"


def test_original_approval_binding_is_not_relaxed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    plan, _digests = _consumer_state(root, route=True)
    with sqlite3.connect(root / "execution-plans.db") as database:
        database.execute(
            "UPDATE execution_approvals SET payload=?",
            (_approval(plan, "0" * 64).model_dump_json(),),
        )
    control = MaintenanceIOControl()
    for tree, paths in _tree_paths(root, control):
        with pytest.raises(KernelError) as error:
            records._plans_and_routes(tree, (), paths, control)
        assert error.value.code == "execution_store_corrupt"


def test_all_cross_store_readers_share_maintenance_checkpoint_and_cas(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "state"
    plan, digests = _consumer_state(root, route=True)
    _failed_process(root, plan)
    control = MaintenanceIOControl()
    opened = []
    requested = {"execution": [], "route": []}

    def workspace_factory(path, **kwargs):
        assert kwargs["read_only"] is True
        assert kwargs["checkpoint"] == control.checkpoint
        opened.append("workspace")
        return SQLiteWorkspaceTransactionStore(path, **kwargs)

    def consumer_factory(kind, store_type):
        def create(path, **kwargs):
            assert kwargs["read_only"] is True
            assert kwargs["checkpoint"] == control.checkpoint
            original = kwargs["read_blob"]

            def read(digest):
                requested[kind].append(digest)
                return original(digest)

            kwargs["read_blob"] = read
            opened.append(kind)
            return store_type(path, **kwargs)

        return create

    monkeypatch.setattr(records, "SQLiteWorkspaceTransactionStore", workspace_factory)
    monkeypatch.setattr(
        records, "SQLiteExecutionPlanStore", consumer_factory("execution", SQLiteExecutionPlanStore)
    )
    monkeypatch.setattr(
        records, "SQLiteActionAuditStore", consumer_factory("route", SQLiteActionAuditStore)
    )
    for tree, paths in _tree_paths(root, control):
        records._plans_and_routes(tree, (), paths, control)
        records._delivery(tree, paths, control)
        records._processes(tree, paths, control)
    assert opened.count("workspace") == 3
    assert opened.count("execution") == 2
    assert opened.count("route") == 1
    assert set(requested["execution"]) == set(digests)
    assert set(requested["route"]) == set(digests)


@pytest.mark.parametrize("reader", ["execution", "route"])
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_new_readers_preserve_control_exception_during_last_chunk(
    tmp_path: Path, monkeypatch, reader: str, reason: str
) -> None:
    root = tmp_path / "state"
    plan, digests = _consumer_state(root, route=reader == "route")
    last_chunk = digests[1]
    control = MaintenanceIOControl()
    failure = KernelError(
        "maintenance_io_cancelled" if reason == "cancel" else "maintenance_io_timeout", "受控停止"
    )
    stopped = False
    original = SQLiteWorkspaceTransactionStore.blob

    def checkpoint():
        if stopped:
            raise failure

    def read(store, digest):
        nonlocal stopped
        body = original(store, digest)
        if digest == last_chunk:
            stopped = True
        return body

    monkeypatch.setattr(control, "checkpoint", checkpoint)
    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, "blob", read)
    for tree, paths in _tree_paths(root, control):
        with SQLiteWorkspaceTransactionStore(
            root / "workspace-transactions", read_only=True, checkpoint=control.checkpoint
        ) as workspace:
            read_blob = records._inventory_reader(workspace, tree, paths, control)
            if reader == "execution":
                with SQLiteExecutionPlanStore(
                    root / "execution-plans.db",
                    read_only=True,
                    read_blob=read_blob,
                    checkpoint=control.checkpoint,
                ) as store:
                    with pytest.raises(KernelError) as error:
                        store.load_plan(plan.plan_id)
            else:
                with SQLiteActionAuditStore(
                    root / "action-audit.db",
                    read_only=True,
                    read_blob=read_blob,
                    checkpoint=control.checkpoint,
                ) as store:
                    with pytest.raises(KernelError) as error:
                        store.routes()
            assert error.value is failure


async def test_new_and_old_history_backup_verify_restore_preserves_original_bytes(
    complete_state, tmp_path: Path
) -> None:
    from harnessix.product_config.state_backup import backup_product_state, verify_product_backup
    from harnessix.product_config.state_restore import restore_product_state

    root, old_prepared = complete_state
    snapshot, blobs = _snapshot()
    workspace_plan = _workspace_plan(snapshot)
    execution = _execution(snapshot)
    with SQLiteWorkspaceTransactionStore(root / "workspace-transactions") as workspace:
        for digest, body in blobs.items():
            workspace.put_blob(digest, body)
        record = workspace.save(
            PreparedWorkspaceTransaction(workspace_plan, {_sha(b"after"): b"after"})
        )
        for state in ("publishing", "interrupted", "publishing", "published"):
            updated = transition_transaction_record(
                record, state=state, cursor=1 if state == "published" else 0, now=utc_now()
            )
            workspace.transition(record, updated)
            record = updated
        original = workspace._db.execute(
            "SELECT transaction_id,sequence,state,payload FROM workspace_transaction_events "
            "ORDER BY transaction_id,sequence"
        ).fetchall()
        with SQLiteExecutionPlanStore(
            root / "execution-plans.db", read_blob=workspace.blob
        ) as store:
            store.save_plan(execution)
            store.record_approval(_approval(execution))
        with SQLiteActionAuditStore(root / "action-audit.db", read_blob=workspace.blob) as store:
            store.save_plan(_route(execution), initial_state="pending_approval")
    original_key = (root / "session-auth/key.v1").read_bytes()
    destination = tmp_path / "closure-backup"
    manifest = await backup_product_state(root, destination)
    assert await verify_product_backup(root, destination) == manifest
    inventory = {item.path: (item.size_bytes, item.sha256) for item in manifest.files}
    for digest, body in blobs.items():
        assert inventory["workspace-transactions/blobs/" + digest] == (len(body), digest)
    await restore_product_state(
        root, destination, restore_id=uuid4(), confirm_backup_id=manifest.backup_id
    )
    assert await verify_product_backup(root, destination) == manifest
    assert (root / "session-auth/key.v1").read_bytes() == original_key
    with SQLiteWorkspaceTransactionStore(
        root / "workspace-transactions", read_only=True
    ) as workspace:
        assert workspace.load(record.transaction_id) == record
        assert workspace.load(old_prepared.plan.transaction_id).plan == old_prepared.plan
        assert (
            original
            == workspace._db.execute(
                "SELECT transaction_id,sequence,state,payload FROM workspace_transaction_events "
                "ORDER BY transaction_id,sequence"
            ).fetchall()
        )
        with SQLiteExecutionPlanStore(
            root / "execution-plans.db", read_only=True, read_blob=workspace.blob
        ) as store:
            assert store.load_plan(execution.plan_id) == execution
        with SQLiteActionAuditStore(
            root / "action-audit.db", read_only=True, read_blob=workspace.blob
        ) as store:
            assert store.load(execution.plan_id).plan == _route(execution)
