from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, ExecutionPlan
from harnessix.execution.planner import build_execution_plan
from harnessix.execution.store import SQLiteExecutionPlanStore
from tests.execution.test_plans import fixtures


def _plan(
    root: Path, *, path_value: str = "/usr/bin", plan_id: UUID | None = None
) -> ExecutionPlan:
    snapshot, capabilities, sandbox, intent, policy, secrets = fixtures(root)
    return build_execution_plan(
        intent,
        snapshot,
        environment={"PATH": path_value},
        secrets=secrets,
        sandbox=sandbox,
        policy=policy,
        capabilities=capabilities,
        plan_id=plan_id,
    )


def _approval(plan: ExecutionPlan) -> ExecutionApprovalCheckpoint:
    return ExecutionApprovalCheckpoint(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        decision=ApprovalRecord(
            outcome=ApprovalOutcome.APPROVED,
            actor="reviewer",
            request_fingerprint=plan.fingerprint,
        ),
    )


def test_plan_and_approval_are_durable_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("before", encoding="utf-8")
    plan = _plan(root)
    path = tmp_path / "private/plans.db"
    with SQLiteExecutionPlanStore(path) as store:
        store.save_plan(plan)
        store.save_plan(plan)
        approval = _approval(plan)
        store.record_approval(approval)
        store.record_approval(approval)

    with SQLiteExecutionPlanStore(path) as reopened:
        assert reopened.load_plan(plan.plan_id) == plan
        assert reopened.load_approval(plan.plan_id) == approval


def test_plan_id_and_approval_are_append_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("before", encoding="utf-8")
    plan = _plan(root)
    with SQLiteExecutionPlanStore(tmp_path / "private/plans.db") as store:
        store.save_plan(plan)
        with pytest.raises(KernelError) as conflict:
            store.save_plan(_plan(root, path_value="/bin", plan_id=plan.plan_id))
        assert conflict.value.code == "execution_plan_conflict"

        approval = _approval(plan)
        store.record_approval(approval)
        changed = approval.model_copy(
            update={"decision": approval.decision.model_copy(update={"actor": "other"})}
        )
        with pytest.raises(KernelError) as approval_conflict:
            store.record_approval(changed)
        assert approval_conflict.value.code == "approval_conflict"


def test_store_fails_closed_on_unknown_schema_and_corrupt_payload(tmp_path: Path) -> None:
    path = tmp_path / "private/plans.db"
    path.parent.mkdir()
    database = sqlite3.connect(path)
    database.execute(
        "CREATE TABLE execution_store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO execution_store_metadata VALUES ('schema_version', '2')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteExecutionPlanStore(path)
    assert version.value.code == "execution_store_version"

    path.unlink()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("before", encoding="utf-8")
    plan = _plan(root)
    with SQLiteExecutionPlanStore(path) as store:
        store.save_plan(plan)
        store._db.execute(  # noqa: SLF001 - 故障注入验证损坏记录失败关闭
            "UPDATE execution_plans SET payload = '{}' WHERE plan_id = ?", (str(plan.plan_id),)
        )
        with pytest.raises(KernelError) as corrupt:
            store.load_plan(plan.plan_id)
        assert corrupt.value.code == "execution_store_corrupt"
