from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from harnessix.agent.ids import new_id
from harnessix.agent.models import EventDraft, ThreadCreated
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_owner import product_action_runtime_lock
from harnessix.product_config.action_recovery import scan_product_action_recovery
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.trusted_actions.test_router import (
    FakeExecutor,
    binding,
    context,
    definition,
    invocation,
)


def test_product_action_runtime_lock_rejects_competing_process(tmp_path: Path) -> None:
    script = (
        "from pathlib import Path; from harnessix.agent.errors import KernelError; "
        "from harnessix.product_config.action_owner import product_action_runtime_lock; "
        "import sys; "
        "\ntry:\n  with product_action_runtime_lock(Path(sys.argv[1])): pass\n"
        "except KernelError as error:\n  print(error.code)\n  raise SystemExit(0)\n"
        "raise SystemExit(2)"
    )
    with product_action_runtime_lock(tmp_path):
        competed = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    assert competed.returncode == 0
    assert competed.stdout.strip() == "action_runtime_busy"


async def test_recovery_scan_repairs_plan_orphan_without_executing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    await sessions.initialize()
    thread_id = new_id()
    await sessions.append(
        thread_id,
        (EventDraft(payload=ThreadCreated(workspace=str(workspace))),),
        expected_sequence=0,
    )
    artifacts = SQLiteArtifactStore(sessions)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    tool = binding(source_id="harnessix.product")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    router.register(definition(tool, executor))
    route = router.plan(invocation(tool), context(workspace))
    plan_id = route.plan.execution.plan_id
    plans._db.execute(  # noqa: SLF001 - 构造Audit已提交而Execution Store未提交的崩溃窗口
        "DELETE FROM execution_plans WHERE plan_id = ?",
        (str(plan_id),),
    )
    orphan_call = uuid4()
    now = "2026-09-20T00:00:00+00:00"
    async with sessions._connection() as database:  # noqa: SLF001 - 构造未提交Session引用窗口
        await database.execute(
            "INSERT INTO agent_artifacts "
            "(artifact_id, thread_id, turn_id, call_id, workspace_scope, manifest_json, "
            "size_bytes, expires_at, state, body, purpose, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, 'action_review', ?)",
            (
                str(uuid4()),
                str(thread_id),
                str(uuid4()),
                str(orphan_call),
                "0" * 64,
                "{}",
                1,
                now,
                b"x",
                now,
            ),
        )
        await database.commit()

    with audit.runtime_owner() as fence:
        report = await scan_product_action_recovery(
            plans=plans,
            audit=audit,
            sessions=sessions,
            artifacts=artifacts,
            supervisor=None,
            fence=fence,
        )

    assert report.scanned_routes == report.repaired_execution_plans == 1
    assert report.invalid_execution_plans == report.session_orphan_references == 0
    assert report.routes_without_session_reference == 1
    assert report.artifact_orphans == 1
    assert plans.load_plan(plan_id) == route.plan.execution
    assert executor.calls == executor.reconciliations == 0
    plans.close()
    audit.close()
