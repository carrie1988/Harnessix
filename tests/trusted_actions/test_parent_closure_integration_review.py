"""父闭包评审回归：真实历史数据库、原Gateway控制与严格代际边界。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.execution.contracts import ExecutionPlan, ExecutionPlanV2, canonical_digest
from harnessix.execution.planner import build_execution_plan_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    action_route_plan_fingerprint,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.execution.test_parent_closure_store import read_blobs
from tests.execution.test_store import _approval, _plan
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FileInput,
    agent_state,
    descriptor,
    runtime_context,
)
from tests.trusted_actions.test_parent_closure_store import parent_route


def _legacy_route(root: Path, current: ActionRoutePlanV2) -> ActionRoutePlan:
    execution = current.execution
    legacy = build_execution_plan_v2(
        execution.intent,
        capture_workspace_snapshot(root),
        environment={"PATH": "/usr/bin"},
        secrets=execution.secrets,
        sandbox=execution.sandbox,
        policy=execution.policy,
        capabilities=execution.capabilities,
        plan_id=execution.plan_id,
    )
    candidate = ActionRoutePlan.model_construct(
        **current.model_dump(exclude={"spec_version", "execution", "invocation", "binding"}),
        execution=legacy,
        invocation=current.invocation,
        binding=current.binding,
    )
    return ActionRoutePlan.model_validate_json(
        candidate.model_copy(
            update={"fingerprint": action_route_plan_fingerprint(candidate)}
        ).model_dump_json(),
        strict=True,
    )


def _untagged(payload: str) -> str:
    value = json.loads(payload)
    del value["spec_version"]
    return json.dumps(value, ensure_ascii=False, indent=2)


def _forbidden(*_: object) -> NoReturn:
    raise AssertionError("旧历史或无效代际不得访问父闭包端口")


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("read_only", [False, True])
def test_real_legacy_execution_db_load_and_approval_keep_untagged_bytes(
    tmp_path: Path, generation: int, read_only: bool
) -> None:
    root = tmp_path / "workspace"
    current, _ = parent_route(root)
    plan = _plan(root) if generation == 1 else _legacy_route(root, current).execution
    payload = _untagged(plan.model_dump_json())
    # 原无discriminator的Reader证明此历史正文合法，摘要无需重签。
    assert TypeAdapter(ExecutionPlan | ExecutionPlanV2).validate_json(payload, strict=True) == plan
    path = tmp_path / "private/plans.db"
    approval = _approval(plan)
    with SQLiteExecutionPlanStore(path) as store:
        store.save_plan(plan)
        store.record_approval(approval)
        store._db.execute(
            "UPDATE execution_plans SET payload=? WHERE plan_id=?", (payload, str(plan.plan_id))
        )
    with SQLiteExecutionPlanStore(
        path, read_only=read_only, read_blob=_forbidden, checkpoint=_forbidden
    ) as store:
        assert type(store.load_plan(plan.plan_id)) is type(plan)
        assert store.load_plan(plan.plan_id) == plan
        assert store.load_approval(plan.plan_id) == approval
        if not read_only:
            store.record_approval(approval)
        assert store._db.execute("SELECT payload FROM execution_plans").fetchone() == (payload,)
        assert store._db.execute(
            "SELECT value FROM execution_store_metadata WHERE key='schema_version'"
        ).fetchone() == ("1",)


@pytest.mark.parametrize("read_only", [False, True])
def test_real_legacy_route_db_load_events_keep_untagged_bytes(
    tmp_path: Path, read_only: bool
) -> None:
    root = tmp_path / "workspace"
    current, _ = parent_route(root)
    route = _legacy_route(root, current)
    payload = _untagged(route.model_dump_json())
    assert ActionRoutePlan.model_validate_json(payload, strict=True) == route
    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path) as store:
        original = store.save_plan(route, initial_state="pending_approval")
        events = store.events(route.execution.plan_id)
        event_rows = store._db.execute("SELECT * FROM action_audit_events").fetchall()
        store._db.execute(
            "UPDATE action_route_plans SET payload=? WHERE plan_id=?",
            (payload, str(route.execution.plan_id)),
        )
    with SQLiteActionAuditStore(
        path, read_only=read_only, read_blob=_forbidden, checkpoint=_forbidden
    ) as store:
        assert store.load(route.execution.plan_id) == original
        assert store.events(route.execution.plan_id) == events
        assert store.active() == (original,)
        assert store._db.execute("SELECT payload FROM action_route_plans").fetchone() == (payload,)
        assert store._db.execute("SELECT * FROM action_audit_events").fetchall() == event_rows
        assert store._db.execute(
            "SELECT value FROM action_audit_metadata WHERE key='schema_version'"
        ).fetchone() == ("2",)


def _damaged(payload: str, damage: str, *, route: bool = False) -> str:
    value = json.loads(payload)
    if damage in {"missing", "strict"}:
        del value["spec_version"]
    else:
        versions: dict[str, object] = {
            "unknown": "harnessix.unknown/v999",
            "null": None,
            "numeric": 1,
            "old1": "harnessix.action-route-plan/v1" if route else "harnessix.execution-plan/v1",
            "old2": "harnessix.execution-plan/v2",
        }
        value["spec_version"] = versions[damage]
    if damage == "strict":
        execution = value["execution"] if route else value
        # 宽松布尔转换会恢复原摘要，因此必须在原strict边界直接拒绝整数零。
        execution["capabilities"]["supports_pty"] = 0
    return json.dumps(value, ensure_ascii=False, indent=2)


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("damage", ["unknown", "null", "numeric", "missing", "old1", "old2"])
def test_execution3_invalid_or_missing_tag_never_falls_back_or_rewrites(
    tmp_path: Path, read_only: bool, damage: str
) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    plan = route.execution
    payload = _damaged(plan.model_dump_json(), damage)
    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path, read_blob=read_blobs(blobs)) as store:
        store.save_plan(plan)
        store._db.execute(
            "UPDATE execution_plans SET payload=? WHERE plan_id=?", (payload, str(plan.plan_id))
        )
    with SQLiteExecutionPlanStore(path, read_only=read_only, read_blob=_forbidden) as store:
        with pytest.raises(KernelError) as error:
            store.load_plan(plan.plan_id)
        assert error.value.code == "execution_store_corrupt"
        assert store._db.execute("SELECT payload FROM execution_plans").fetchone() == (payload,)


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("damage", ["unknown", "null", "numeric", "missing", "old1"])
def test_route2_invalid_or_missing_tag_never_falls_back_or_rewrites(
    tmp_path: Path, read_only: bool, damage: str
) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    payload = _damaged(route.model_dump_json(), damage, route=True)
    path = tmp_path / "private/audit.db"
    with SQLiteActionAuditStore(path, read_blob=read_blobs(blobs)) as store:
        store.save_plan(route, initial_state="pending_approval")
        event_rows = store._db.execute("SELECT * FROM action_audit_events").fetchall()
        store._db.execute(
            "UPDATE action_route_plans SET payload=? WHERE plan_id=?",
            (payload, str(route.execution.plan_id)),
        )
    with SQLiteActionAuditStore(path, read_only=read_only, read_blob=_forbidden) as store:
        for read in (store.load, store.events):
            with pytest.raises(KernelError) as error:
                read(route.execution.plan_id)
            assert error.value.code == "action_audit_store_corrupt"
        assert store._db.execute("SELECT payload FROM action_route_plans").fetchone() == (payload,)
        assert store._db.execute("SELECT * FROM action_audit_events").fetchall() == event_rows


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("damage", ["strict", "unknown", "null", "numeric"])
def test_legacy_execution_invalid_tag_or_strict_data_is_not_repaired(
    tmp_path: Path, generation: int, damage: str
) -> None:
    root = tmp_path / "workspace"
    current, _ = parent_route(root)
    plan = _plan(root) if generation == 1 else _legacy_route(root, current).execution
    payload = _damaged(plan.model_dump_json(), damage)
    with pytest.raises(ValidationError):
        TypeAdapter(ExecutionPlan | ExecutionPlanV2).validate_json(payload, strict=True)
    with SQLiteExecutionPlanStore(tmp_path / "private/plans.db") as store:
        store.save_plan(plan)
        store._db.execute(
            "UPDATE execution_plans SET payload=? WHERE plan_id=?", (payload, str(plan.plan_id))
        )
        with pytest.raises(KernelError) as error:
            store.load_plan(plan.plan_id)
        assert error.value.code == "execution_store_corrupt"
        assert store._db.execute("SELECT payload FROM execution_plans").fetchone() == (payload,)


@pytest.mark.parametrize("damage", ["strict", "unknown", "null", "numeric"])
def test_legacy_route_invalid_tag_or_strict_data_is_not_repaired(
    tmp_path: Path, damage: str
) -> None:
    root = tmp_path / "workspace"
    current, _ = parent_route(root)
    route = _legacy_route(root, current)
    payload = _damaged(route.model_dump_json(), damage, route=True)
    with pytest.raises(ValidationError):
        ActionRoutePlan.model_validate_json(payload, strict=True)
    with SQLiteActionAuditStore(tmp_path / "private/audit.db") as store:
        store.save_plan(route, initial_state="pending_approval")
        store._db.execute(
            "UPDATE action_route_plans SET payload=? WHERE plan_id=?",
            (payload, str(route.execution.plan_id)),
        )
        with pytest.raises(KernelError) as error:
            store.events(route.execution.plan_id)
        assert error.value.code == "action_audit_store_corrupt"
        assert store._db.execute("SELECT payload FROM action_route_plans").fetchone() == (payload,)


@pytest.mark.parametrize("shape", ["array", "object"])
@pytest.mark.parametrize("history", ["execution1", "execution2", "route1"])
@pytest.mark.parametrize("read_only", [False, True])
def test_deep_invalid_legacy_db_payload_keeps_original_corruption_classification(
    tmp_path: Path, shape: str, history: str, read_only: bool
) -> None:
    payload = "[" * 20000 + "0" + "]" * 20000
    if shape == "object":
        payload = '{"nested":' * 20000 + "0" + "}" * 20000
    with pytest.raises(RecursionError):
        json.loads(payload)
    # 原模型JSON Reader受控拒绝深层非法数据，不能泄漏预解析的递归异常。
    with pytest.raises(ValidationError):
        TypeAdapter(ExecutionPlan | ExecutionPlanV2).validate_json(payload, strict=True)
    root = tmp_path / "workspace"
    current, _ = parent_route(root)
    if history.startswith("execution"):
        plan = _plan(root) if history == "execution1" else _legacy_route(root, current).execution
        path = tmp_path / "private/plans.db"
        with SQLiteExecutionPlanStore(path) as plans:
            plans.save_plan(plan)
            plans._db.execute(
                "UPDATE execution_plans SET payload=? WHERE plan_id=?",
                (payload, str(plan.plan_id)),
            )
        with SQLiteExecutionPlanStore(path, read_only=read_only, read_blob=_forbidden) as plans:
            with pytest.raises(KernelError) as error:
                plans.load_plan(plan.plan_id)
            assert error.value.code == "execution_store_corrupt"
            assert plans._db.execute("SELECT payload FROM execution_plans").fetchone() == (payload,)
    else:
        route = _legacy_route(root, current)
        with pytest.raises(ValidationError):
            ActionRoutePlan.model_validate_json(payload, strict=True)
        path = tmp_path / "private/audit.db"
        with SQLiteActionAuditStore(path) as audit:
            audit.save_plan(route, initial_state="pending_approval")
            audit._db.execute(
                "UPDATE action_route_plans SET payload=? WHERE plan_id=?",
                (payload, str(route.execution.plan_id)),
            )
        with SQLiteActionAuditStore(path, read_only=read_only, read_blob=_forbidden) as audit:
            for read in (audit.load, audit.events):
                with pytest.raises(KernelError) as error:
                    read(route.execution.plan_id)
                assert error.value.code == "action_audit_store_corrupt"
            assert audit._db.execute("SELECT payload FROM action_route_plans").fetchone() == (
                payload,
            )


@pytest.mark.parametrize("store_kind", ["execution", "audit"])
def test_parent_reader_recursion_error_is_not_classified_as_json_corruption(
    tmp_path: Path, store_kind: str
) -> None:
    route, blobs = parent_route(tmp_path / "workspace")
    failure = RecursionError("上游读取控制")

    def stop(_: str) -> bytes:
        raise failure

    if store_kind == "execution":
        with SQLiteExecutionPlanStore(
            tmp_path / "private/plans.db", read_blob=read_blobs(blobs)
        ) as plans:
            plans.save_plan(route.execution)
            plans._read_blob = stop
            with pytest.raises(RecursionError) as error:
                plans.load_plan(route.execution.plan_id)
            assert error.value is failure
    else:
        with SQLiteActionAuditStore(
            tmp_path / "private/audit.db", read_blob=read_blobs(blobs)
        ) as audit:
            audit.save_plan(route, initial_state="pending_approval")
            audit._read_blob = stop
            for read in (audit.load, audit.events):
                with pytest.raises(RecursionError) as error:
                    read(route.execution.plan_id)
                assert error.value is failure


@contextmanager
def _gateway(
    root: Path, checkpoint: Callable[[], None]
) -> Iterator[
    tuple[RouterBackedAgentActionGateway, SQLiteExecutionPlanStore, SQLiteActionAuditStore]
]:
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    blobs: dict[str, bytes] = {}
    ports = WorkspaceSnapshotPorts(blobs.__setitem__, read_blobs(blobs))
    context = replace(runtime_context(root), snapshot_ports=ports, checkpoint=checkpoint)
    tool = descriptor()
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=tool.name,
        tool_version=tool.version,
        tool_fingerprint=tool_fingerprint(tool),
        input_schema_sha256=canonical_digest(tool.input_schema),
        effect_class=tool.effect_class,
        risk_level=tool.risk_level,
        recovery_mode="durable_ledger",
        executor_id="product.workspace-patch",
    )

    def resolve(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        checked = FileInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="workspace",
                    access="write",
                    identifier={
                        "location": "workspace",
                        "path": checked.path,
                    },
                ),
            ),
            workspace_resources=(WorkspaceResourceRequest(path=checked.path, access="write"),),
        )

    with (
        SQLiteExecutionPlanStore(
            root.parent / "state/plans.db", read_blob=ports.read_blob
        ) as plans,
        SQLiteActionAuditStore(root.parent / "state/audit.db", read_blob=ports.read_blob) as audit,
    ):
        router = TrustedActionRouter(
            plans=plans, audit=audit, workspace_root=lambda _: root, snapshot_ports=ports
        )
        router.register(
            TrustedActionDefinition(
                binding, FileInput, resolve, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
            )
        )
        gateway = RouterBackedAgentActionGateway(router, (tool,), lambda *_: context)
        try:
            yield gateway, plans, audit
        finally:
            gateway.close()


@pytest.mark.parametrize(
    "failure",
    [
        KernelError("host_cancelled", "宿主取消"),
        TimeoutError("宿主期限"),
        ValueError("宿主控制"),
        OSError("宿主控制"),
        RecursionError("宿主控制"),
        TurnCancelled(),
        asyncio.CancelledError(),
    ],
)
async def test_original_gateway_preserves_host_checkpoint_exception_identity(
    tmp_path: Path, failure: BaseException
) -> None:
    calls = 0

    def checkpoint() -> None:
        nonlocal calls
        calls += 1
        raise failure

    root = tmp_path / "workspace"
    with _gateway(root, checkpoint) as (gateway, plans, audit):
        thread, turn, call = agent_state(root)
        with pytest.raises(type(failure)) as error:
            await gateway.prepare(thread, turn, call, CancelToken())
        assert error.value is failure and calls > 0
        assert plans._db.execute("SELECT COUNT(*) FROM execution_plans").fetchone() == (0,)
        assert audit._db.execute("SELECT COUNT(*) FROM action_route_plans").fetchone() == (0,)


@pytest.mark.parametrize("control", ["none", "token", "task"])
async def test_original_gateway_composes_host_token_and_parent_task_control(
    tmp_path: Path, control: str
) -> None:
    token = CancelToken()
    calls = 0

    def checkpoint() -> None:
        nonlocal calls
        calls += 1
        if control == "token":
            token.cancel()
        elif control == "task":
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    root = tmp_path / "workspace"
    with _gateway(root, checkpoint) as (gateway, plans, audit):
        thread, turn, call = agent_state(root)
        task = asyncio.create_task(gateway.prepare(thread, turn, call, token))
        if control == "none":
            assert isinstance(await task, TrustedActionApprovalRequestContent)
        else:
            with pytest.raises(TurnCancelled if control == "token" else asyncio.CancelledError):
                await task
            assert plans._db.execute("SELECT COUNT(*) FROM execution_plans").fetchone() == (0,)
            assert audit._db.execute("SELECT COUNT(*) FROM action_route_plans").fetchone() == (0,)
        assert calls > 0
