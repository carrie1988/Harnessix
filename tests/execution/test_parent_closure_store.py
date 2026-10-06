from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord, PolicyDecisionKind
from harnessix.execution.contracts import (
    ExecutionApprovalCheckpoint,
    ExecutionPlanV2,
    ExecutionPolicyBinding,
    SandboxBindingV2,
)
from harnessix.execution.planner import (
    build_capability_evidence_v2,
    build_execution_plan_v2,
    build_execution_plan_v3,
)
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2
from tests.execution.test_plans import fixtures
from tests.execution.test_store import _approval, _plan


def parent_plan(root: Path, *, leaves: int = 1) -> tuple[ExecutionPlanV3, dict[str, bytes]]:
    root.mkdir()
    (root / "main.py").write_text("before", encoding="utf-8")
    _, _, _, intent, policy, secrets = fixtures(root)
    requests = []
    for index in range(leaves):
        path = f"branch-{index}/nested/leaf.py"
        target = root / path
        target.parent.mkdir(parents=True)
        target.write_text("value", encoding="utf-8")
        requests.append(WorkspaceResourceRequest(path=path, access="write"))
    blobs: dict[str, bytes] = {}
    snapshot = capture_workspace_snapshot_v2(
        root,
        resources=requests,
        checkpoint=lambda: None,
        write_blob=blobs.__setitem__,
        read_blob=blobs.__getitem__,
    )
    capabilities = build_capability_evidence_v2(
        platform=snapshot.platform,
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest="1" * 64,
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest="2" * 64,
    )
    plan = build_execution_plan_v3(
        intent,
        snapshot,
        environment={"PATH": "/usr/bin"},
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
    )
    return plan, blobs


def read_blobs(blobs: dict[str, bytes]) -> Callable[[str], bytes]:
    def read(digest: str) -> bytes:
        if digest not in blobs:
            raise KernelError("delivery_blob_corrupt", "验证对象缺失")
        return blobs[digest]

    return read


def schema(store: SQLiteExecutionPlanStore) -> str:
    return store._db.execute(  # noqa: SLF001 - 检查真实数据库元数据
        "SELECT value FROM execution_store_metadata WHERE key = 'schema_version'"
    ).fetchone()[0]


@pytest.mark.parametrize("leaves", [1, 128, 255])
def test_v3_complete_roundtrip_approval_and_lazy_schema(tmp_path: Path, leaves: int) -> None:
    plan, blobs = parent_plan(tmp_path / "workspace", leaves=leaves)
    path = tmp_path / "private/plans.db"
    reads: list[str] = []

    def read(digest: str) -> bytes:
        reads.append(digest)
        return read_blobs(blobs)(digest)

    with SQLiteExecutionPlanStore(path, read_blob=read) as store:
        assert schema(store) == "1"
        store.save_plan(plan)
        assert schema(store) == "2"
        store.save_plan(plan)
        approval = _approval(plan)
        store.record_approval(approval)
        assert store.load_approval(plan.plan_id) == approval
        assert set(reads) == set(blobs)
    with SQLiteExecutionPlanStore(path, read_only=True, read_blob=read) as store:
        loaded = store.load_plan(plan.plan_id)
        assert type(loaded) is ExecutionPlanV3
        assert type(loaded.workspace) is WorkspaceSnapshotV2
        assert loaded == plan
        assert loaded.model_dump_json() == plan.model_dump_json()
        assert store.load_approval(plan.plan_id) == approval
        assert schema(store) == "2"


@pytest.mark.parametrize("operation", ["save", "load", "approve", "approval"])
@pytest.mark.parametrize("damage", ["no_port", "manifest", "chunk", "bytes"])
def test_v3_fails_closed_at_every_store_boundary(
    tmp_path: Path, operation: str, damage: str
) -> None:
    plan, blobs = parent_plan(tmp_path / "workspace")
    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path, read_blob=read_blobs(blobs)) as store:
        if operation != "save":
            store.save_plan(plan)
        if operation == "approval":
            store.record_approval(_approval(plan))
    if damage == "manifest":
        del blobs[plan.workspace.parent_closure.sha256]
    elif damage == "chunk":
        manifest = json.loads(blobs[plan.workspace.parent_closure.sha256])
        del blobs[manifest["chunks"][0]["sha256"]]
    elif damage == "bytes":
        blobs[plan.workspace.parent_closure.sha256] = b"{}"
    read = None if damage == "no_port" else read_blobs(blobs)
    with SQLiteExecutionPlanStore(path, read_blob=read) as store:
        with pytest.raises(KernelError) as error:
            if operation == "save":
                store.save_plan(plan)
            elif operation == "load":
                store.load_plan(plan.plan_id)
            elif operation == "approve":
                store.record_approval(_approval(plan))
            else:
                store.load_approval(plan.plan_id)
        assert error.value.code == (
            "execution_plan_invalid" if operation == "save" else "execution_store_corrupt"
        )
        assert not store._db.in_transaction  # noqa: SLF001 - 控制失败必须回滚
        if operation == "save":
            assert schema(store) == "1"
            assert store._db.execute("SELECT COUNT(*) FROM execution_plans").fetchone()[0] == 0  # noqa: SLF001
        elif operation == "approve":
            assert store._db.execute("SELECT COUNT(*) FROM execution_approvals").fetchone()[0] == 0  # noqa: SLF001


@pytest.mark.parametrize("operation", ["save", "load", "approve", "approval"])
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
def test_control_errors_are_identical_and_never_mapped(
    tmp_path: Path, operation: str, origin: str, failure: BaseException
) -> None:
    plan, blobs = parent_plan(tmp_path / "workspace")
    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path, read_blob=read_blobs(blobs)) as store:
        if operation != "save":
            store.save_plan(plan)
        if operation == "approval":
            store.record_approval(_approval(plan))

    def stop(*_: object) -> bytes:
        raise failure

    with SQLiteExecutionPlanStore(
        path,
        read_blob=stop if origin == "reader" else read_blobs(blobs),
        checkpoint=stop if origin == "checkpoint" else None,
    ) as store:
        with pytest.raises(type(failure)) as error:
            if operation == "save":
                store.save_plan(plan)
            elif operation == "load":
                store.load_plan(plan.plan_id)
            elif operation == "approve":
                store.record_approval(_approval(plan))
            else:
                store.load_approval(plan.plan_id)
        assert error.value is failure
        assert not store._db.in_transaction  # noqa: SLF001
        if operation == "save":
            assert schema(store) == "1"


def test_schema_promotion_and_new_plan_are_one_transaction(tmp_path: Path) -> None:
    plan, blobs = parent_plan(tmp_path / "workspace")
    with SQLiteExecutionPlanStore(
        tmp_path / "private/plans.db", read_blob=read_blobs(blobs)
    ) as store:
        store._db.execute(  # noqa: SLF001 - 元数据写入故障注入
            "CREATE TRIGGER reject_promotion BEFORE UPDATE ON execution_store_metadata "
            "BEGIN SELECT RAISE(ABORT, 'promotion failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.save_plan(plan)
        assert schema(store) == "1"
        assert store._db.execute("SELECT COUNT(*) FROM execution_plans").fetchone()[0] == 0  # noqa: SLF001


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("version", ["1", "2", "99"])
def test_explicit_schema_dispatch_without_readonly_migration(
    tmp_path: Path, read_only: bool, version: str
) -> None:
    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path) as store:
        store._db.execute(  # noqa: SLF001
            "UPDATE execution_store_metadata SET value = ?", (version,)
        )
    if version == "99":
        with pytest.raises(KernelError) as error:
            SQLiteExecutionPlanStore(path, read_only=read_only)
        assert error.value.code == "execution_store_version"
    else:
        with SQLiteExecutionPlanStore(path, read_only=read_only) as store:
            assert schema(store) == version


@pytest.mark.parametrize("generation", [1, 2])
def test_legacy_bytes_and_schema_are_unchanged(tmp_path: Path, generation: int) -> None:
    new, _ = parent_plan(tmp_path / "workspace")
    plan = _plan(tmp_path / "workspace")
    if generation == 2:
        plan = build_execution_plan_v2(
            new.intent,
            capture_workspace_snapshot(tmp_path / "workspace"),
            environment={"PATH": "/usr/bin"},
            secrets=new.secrets,
            sandbox=new.sandbox,
            policy=new.policy,
            capabilities=new.capabilities,
            plan_id=new.plan_id,
        )
    approval = _approval(plan)

    def forbidden(*_: object) -> bytes:
        pytest.fail("旧代际不得调用父闭包端口")

    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path, read_blob=forbidden, checkpoint=forbidden) as store:
        store.save_plan(plan)
        store.record_approval(approval)
        assert store.load_plan(plan.plan_id).model_dump_json() == plan.model_dump_json()
        assert schema(store) == "1"
        stored = store._db.execute("SELECT payload FROM execution_plans").fetchone()[0]  # noqa: SLF001
        assert stored == plan.model_dump_json()
    with SQLiteExecutionPlanStore(path, read_only=True) as store:
        assert store.load_approval(plan.plan_id) == approval
        assert schema(store) == "1"


def test_v3_contract_rejects_downgrades_forgery_and_wrong_approval(tmp_path: Path) -> None:
    plan, blobs = parent_plan(tmp_path / "workspace")
    with pytest.raises(ValidationError):
        ExecutionPlanV2.model_validate_json(plan.model_dump_json())
    wire = json.loads(plan.model_dump_json())
    wire["spec_version"] = "harnessix.execution-plan/v2"
    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path, read_blob=read_blobs(blobs)) as store:
        with pytest.raises(KernelError) as error:
            store.save_plan(plan.model_copy(update={"spec_version": wire["spec_version"]}))
        assert error.value.code == "execution_plan_invalid"
        with pytest.raises(KernelError) as error:
            store.save_plan(plan.model_copy(update={"fingerprint": "0" * 64}))
        assert error.value.code == "execution_plan_invalid"
        store.save_plan(plan)
        fingerprint = "3" * 64
        approval = ExecutionApprovalCheckpoint(
            plan_id=plan.plan_id,
            plan_fingerprint=fingerprint,
            decision=ApprovalRecord(
                outcome=ApprovalOutcome.APPROVED,
                actor="reviewer",
                request_fingerprint=fingerprint,
            ),
        )
        with pytest.raises(KernelError) as error:
            store.record_approval(approval)
        assert error.value.code == "approval_plan_mismatch"
        store._db.execute("UPDATE execution_plans SET payload = ?", (json.dumps(wire),))  # noqa: SLF001
        with pytest.raises(KernelError) as error:
            store.load_plan(plan.plan_id)
        assert error.value.code == "execution_store_corrupt"


def test_v3_inherits_capability_policy_environment_and_secret_validators(tmp_path: Path) -> None:
    plan, _ = parent_plan(tmp_path / "workspace")
    for change in (
        {"environment": [plan.environment[0].model_dump(mode="json")] * 2},
        {"sandbox": {**plan.sandbox.model_dump(mode="json"), "capability_digest": "0" * 64}},
        {"secrets": [plan.secrets[0].model_dump(mode="json")] * 2},
    ):
        with pytest.raises(ValidationError):
            ExecutionPlanV3.model_validate_json(
                json.dumps({**plan.model_dump(mode="json"), **change})
            )
    assert isinstance(plan.policy, ExecutionPolicyBinding)
    assert plan.policy.decision is PolicyDecisionKind.REQUIRE_APPROVAL
