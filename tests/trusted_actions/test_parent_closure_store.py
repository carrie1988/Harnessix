from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.planner import build_execution_plan_v2
from harnessix.trusted_actions.contracts import (
    ActionRoutePlan,
    CodingActionInvocation,
    action_audit_event_digest,
    action_route_plan_fingerprint,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2, ActionRouteSnapshotV2
from harnessix.workspace.snapshot import capture_workspace_snapshot
from tests.execution.test_parent_closure_store import parent_plan, read_blobs


def parent_route(root: Path, *, leaves: int = 1) -> tuple[ActionRoutePlanV2, dict[str, bytes]]:
    execution, blobs = parent_plan(root, leaves=leaves)
    intent = execution.intent
    invocation = CodingActionInvocation(
        invocation_id=execution.plan_id,
        **intent.model_dump(exclude={"spec_version", "effect_class", "risk_level"}),
    )
    binding = build_trusted_tool_binding(
        source=intent.source,
        source_id=intent.source_id,
        tool=intent.tool,
        tool_version=intent.tool_version,
        tool_fingerprint=intent.tool_fingerprint,
        input_schema_sha256="4" * 64,
        effect_class=intent.effect_class,
        risk_level=intent.risk_level,
        recovery_mode="durable_ledger",
        executor_id="test.file",
    )
    candidate = ActionRoutePlanV2.model_construct(
        invocation=invocation,
        binding=binding,
        resources=(),
        resources_sha256=canonical_digest([]),
        execution=execution,
        fingerprint="0" * 64,
    )
    route = ActionRoutePlanV2.model_validate_json(
        candidate.model_copy(
            update={"fingerprint": action_route_plan_fingerprint(candidate)}
        ).model_dump_json()
    )
    return route, blobs


def schema(store: SQLiteActionAuditStore) -> str:
    return store._db.execute(  # noqa: SLF001
        "SELECT value FROM action_audit_metadata WHERE key = 'schema_version'"
    ).fetchone()[0]


@pytest.mark.parametrize("leaves", [1, 128, 255])
def test_route_v2_roundtrip_transition_and_event_generation(tmp_path: Path, leaves: int) -> None:
    route, blobs = parent_route(tmp_path / "workspace", leaves=leaves)
    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path, read_blob=read_blobs(blobs)) as store:
        assert schema(store) == "2"
        snapshot = store.save_plan(route, initial_state="pending_approval")
        assert schema(store) == "3"
        assert type(snapshot) is ActionRouteSnapshotV2
        assert snapshot.plan == route
        assert store.save_plan(route, initial_state="pending_approval") == snapshot
        ready = store.transition(
            route.execution.plan_id,
            expected={"pending_approval"},
            target="ready",
            approval_outcome=ApprovalOutcome.APPROVED,
            approval_actor="reviewer",
        )
        assert type(ready) is ActionRouteSnapshotV2
        assert ready.plan.model_dump_json() == route.model_dump_json()
        events = store.events(route.execution.plan_id)
        assert len(events) == 2
        assert all(event.spec_version == "harnessix.action-audit-event/v1" for event in events)
        assert all(event.plan_fingerprint == route.fingerprint for event in events)
        assert store.active() == (ready,)
    with SQLiteActionAuditStore(path, read_only=True, read_blob=read_blobs(blobs)) as store:
        assert store.load(route.execution.plan_id) == ready
        assert store.events(route.execution.plan_id) == events
        assert schema(store) == "3"


@pytest.mark.parametrize("operation", ["save", "load", "transition", "events", "active"])
@pytest.mark.parametrize("damage", ["no_port", "manifest", "chunk", "bytes"])
def test_route_v2_all_consumers_reject_incomplete_parent_history(
    tmp_path: Path, operation: str, damage: str
) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path, read_blob=read_blobs(blobs)) as store:
        if operation != "save":
            store.save_plan(route, initial_state="pending_approval")
    if damage == "manifest":
        del blobs[route.execution.workspace.parent_closure.sha256]
    elif damage == "chunk":
        manifest = json.loads(blobs[route.execution.workspace.parent_closure.sha256])
        del blobs[manifest["chunks"][0]["sha256"]]
    elif damage == "bytes":
        blobs[route.execution.workspace.parent_closure.sha256] = b"{}"
    with SQLiteActionAuditStore(
        path, read_blob=None if damage == "no_port" else read_blobs(blobs)
    ) as store:
        with pytest.raises(KernelError) as error:
            if operation == "save":
                store.save_plan(route, initial_state="pending_approval")
            elif operation == "load":
                store.load(route.execution.plan_id)
            elif operation == "transition":
                store.transition(
                    route.execution.plan_id, expected={"pending_approval"}, target="ready"
                )
            elif operation == "events":
                store.events(route.execution.plan_id)
            else:
                store.active()
        assert error.value.code == (
            "action_route_plan_invalid" if operation == "save" else "action_audit_store_corrupt"
        )
        assert not store._db.in_transaction  # noqa: SLF001
        if operation == "save":
            assert schema(store) == "2"
            assert store._db.execute("SELECT COUNT(*) FROM action_audit_events").fetchone()[0] == 0  # noqa: SLF001
        elif operation == "transition":
            assert store._db.execute("SELECT COUNT(*) FROM action_audit_events").fetchone()[0] == 1  # noqa: SLF001


@pytest.mark.parametrize("operation", ["save", "load", "transition", "events", "active"])
@pytest.mark.parametrize("origin", ["checkpoint", "reader"])
@pytest.mark.parametrize(
    "failure",
    [
        KernelError("operation_cancelled", "取消"),
        TimeoutError(),
        ValueError(),
        asyncio.CancelledError(),
    ],
)
def test_route_control_errors_preserve_identity(
    tmp_path: Path, operation: str, origin: str, failure: BaseException
) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path, read_blob=read_blobs(blobs)) as store:
        if operation != "save":
            store.save_plan(route, initial_state="pending_approval")

    def stop(*_: object) -> bytes:
        raise failure

    with SQLiteActionAuditStore(
        path,
        read_blob=stop if origin == "reader" else read_blobs(blobs),
        checkpoint=stop if origin == "checkpoint" else None,
    ) as store:
        with pytest.raises(type(failure)) as error:
            if operation == "save":
                store.save_plan(route, initial_state="pending_approval")
            elif operation == "load":
                store.load(route.execution.plan_id)
            elif operation == "transition":
                store.transition(
                    route.execution.plan_id, expected={"pending_approval"}, target="ready"
                )
            elif operation == "events":
                store.events(route.execution.plan_id)
            else:
                store.active()
        assert error.value is failure
        assert not store._db.in_transaction  # noqa: SLF001
        if operation == "save":
            assert schema(store) == "2"


def test_route_schema_promotion_is_atomic(tmp_path: Path) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    with SQLiteActionAuditStore(
        tmp_path / "private/audit.db", read_blob=read_blobs(blobs)
    ) as store:
        store._db.execute(  # noqa: SLF001
            "CREATE TRIGGER reject_promotion BEFORE UPDATE ON action_audit_metadata "
            "BEGIN SELECT RAISE(ABORT, 'promotion failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.save_plan(route, initial_state="pending_approval")
        assert schema(store) == "2"
        for table in ("action_route_plans", "action_route_snapshots", "action_audit_events"):
            assert store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: SLF001


@pytest.mark.parametrize("version", ["1", "2", "3", "99"])
def test_readonly_accepts_actual_historical_generations_without_migration(
    tmp_path: Path, version: str
) -> None:
    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path) as store:
        store._db.execute("UPDATE action_audit_metadata SET value = ?", (version,))  # noqa: SLF001
    if version == "99":
        with pytest.raises(KernelError) as error:
            SQLiteActionAuditStore(path, read_only=True)
        assert error.value.code == "action_audit_store_version"
    else:
        with SQLiteActionAuditStore(path, read_only=True) as store:
            assert schema(store) == version


def test_route_contract_strict_generation_and_inherited_bindings(tmp_path: Path) -> None:
    route, _ = parent_route(tmp_path / "workspace")
    with pytest.raises(ValidationError):
        ActionRoutePlan.model_validate_json(route.model_dump_json())
    assert route.fingerprint == action_route_plan_fingerprint(route)
    for change in (
        {"spec_version": "harnessix.action-route-plan/v1"},
        {"resources_sha256": "0" * 64},
        {"fingerprint": "0" * 64},
        {"invocation": {**route.invocation.model_dump(mode="json"), "tool_version": "other"}},
        {"unknown": True},
    ):
        with pytest.raises(ValidationError):
            ActionRoutePlanV2.model_validate_json(
                json.dumps({**route.model_dump(mode="json"), **change})
            )


def test_legacy_route_bytes_owner_schema_and_events_are_unchanged(tmp_path: Path) -> None:
    new, _ = parent_route(tmp_path / "workspace")
    plan = new.execution
    execution = build_execution_plan_v2(
        plan.intent,
        capture_workspace_snapshot(tmp_path / "workspace"),
        environment={"PATH": "/usr/bin"},
        secrets=plan.secrets,
        sandbox=plan.sandbox,
        policy=plan.policy,
        capabilities=plan.capabilities,
        plan_id=plan.plan_id,
    )
    candidate = ActionRoutePlan.model_construct(
        **new.model_dump(exclude={"spec_version", "execution", "invocation", "binding"}),
        invocation=new.invocation,
        binding=new.binding,
        execution=execution,
    )
    route = ActionRoutePlan.model_validate_json(
        candidate.model_copy(
            update={"fingerprint": action_route_plan_fingerprint(candidate)}
        ).model_dump_json()
    )

    def forbidden(*_: object) -> bytes:
        pytest.fail("旧路由不得调用父闭包端口")

    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path, read_blob=forbidden, checkpoint=forbidden) as store:
        assert schema(store) == "2"
        snapshot = store.save_plan(route, initial_state="pending_approval")
        assert snapshot.spec_version == "harnessix.action-route-snapshot/v1"
        assert snapshot.plan.model_dump_json() == route.model_dump_json()
        stored = store._db.execute("SELECT payload FROM action_route_plans").fetchone()[0]  # noqa: SLF001
        assert stored == route.model_dump_json()
        assert schema(store) == "2"
        events = store.events(route.execution.plan_id)
    with SQLiteActionAuditStore(path, read_only=True) as store:
        assert store.load(route.execution.plan_id) == snapshot
        assert store.events(route.execution.plan_id) == events
        assert schema(store) == "2"


@pytest.mark.parametrize("operation", ["load", "events", "save", "transition"])
def test_validly_hashed_wrong_historical_binding_is_not_resigned(
    tmp_path: Path, operation: str
) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    with SQLiteActionAuditStore(
        tmp_path / "private/audit.db", read_blob=read_blobs(blobs)
    ) as store:
        store.save_plan(route, initial_state="pending_approval")
        original = store.events(route.execution.plan_id)[0]
        wrong = original.model_copy(update={"plan_fingerprint": "0" * 64})
        wrong = wrong.model_copy(update={"digest": action_audit_event_digest(wrong)})
        payload = wrong.model_dump_json()
        store._db.execute(  # noqa: SLF001 - 注入可独立验摘要但未绑定当前计划的历史
            "UPDATE action_audit_events SET digest = ?, payload = ?", (wrong.digest, payload)
        )
        store._db.execute(  # noqa: SLF001
            "UPDATE action_route_snapshots SET last_event_digest = ?", (wrong.digest,)
        )
        with pytest.raises(KernelError) as error:
            if operation == "load":
                store.load(route.execution.plan_id)
            elif operation == "events":
                store.events(route.execution.plan_id)
            elif operation == "save":
                store.save_plan(route, initial_state="pending_approval")
            else:
                store.transition(
                    route.execution.plan_id, expected={"pending_approval"}, target="ready"
                )
        assert error.value.code == "action_audit_store_corrupt"
        assert store._db.execute(  # noqa: SLF001
            "SELECT digest, payload FROM action_audit_events"
        ).fetchall() == [(wrong.digest, payload)]
        assert store._db.execute(  # noqa: SLF001
            "SELECT state, sequence FROM action_route_snapshots"
        ).fetchall() == [("pending_approval", 1)]
