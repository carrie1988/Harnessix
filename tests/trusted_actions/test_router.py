from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
)
from harnessix.execution.contracts import SandboxBindingV2, canonical_digest
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    CodingActionInvocation,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import WorkspaceResourceRequest


class FileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=256)


@dataclass
class FakeExecutor:
    result: ActionExecutionOutcome
    reconciled: ActionExecutionOutcome | None = None
    calls: int = 0
    reconciliations: int = 0

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.calls += 1
        FileInput.model_validate(arguments)
        return self.result

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.reconciliations += 1
        FileInput.model_validate(arguments)
        assert self.reconciled is not None
        return self.reconciled


@dataclass
class CrashExecutor:
    marker: Path

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        FileInput.model_validate(arguments)
        with self.marker.open("ab") as stream:
            stream.write(b"effect\n")
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(73)

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        FileInput.model_validate(arguments)
        if self.marker.read_bytes() == b"effect\n":
            return ActionExecutionOutcome(kind="succeeded", output={"reconciled": True})
        return ActionExecutionOutcome(kind="manual_intervention", error_code="effect_fact_missing")


def _action_crash_worker(
    plans_path: str,
    audit_path: str,
    workspace: str,
    plan_id: str,
    marker: str,
) -> None:
    root = Path(workspace)
    plans = SQLiteExecutionPlanStore(plans_path)
    audit = SQLiteActionAuditStore(audit_path)
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    actions = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: root)
    actions.register(definition(tool, CrashExecutor(Path(marker))))
    asyncio.run(actions.execute(UUID(plan_id)))


def context(root: Path) -> ActionPlanningContext:
    capabilities = build_capability_evidence_v2(
        platform="windows" if __import__("os").name == "nt" else "posix",
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest("test-native-provider"),
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest("test-profile"),
    )
    return ActionPlanningContext(
        workspace_root=root,
        sandbox=sandbox,
        capabilities=capabilities,
    )


def binding(
    *,
    source: str = "builtin",
    source_id: str = "harnessix",
    effect: EffectClass = EffectClass.READ_ONLY,
    risk: RiskLevel = RiskLevel.LOW,
    recovery: str = "none",
) -> TrustedToolBinding:
    return build_trusted_tool_binding(
        source=source,  # type: ignore[arg-type]
        source_id=source_id,
        tool="file.access",
        tool_version="1",
        tool_fingerprint=canonical_digest({"tool": source, "source_id": source_id}),
        input_schema_sha256=canonical_digest(FileInput.model_json_schema()),
        effect_class=effect,
        risk_level=risk,
        recovery_mode=recovery,  # type: ignore[arg-type]
        executor_id="test.file",
    )


def definition(tool: TrustedToolBinding, executor: FakeExecutor) -> TrustedActionDefinition:
    access = "read" if tool.effect_class is EffectClass.READ_ONLY else "write"

    def resolve(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        parsed = FileInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="workspace",
                    access=access,  # type: ignore[arg-type]
                    identifier={"location": "workspace", "path": parsed.path},
                ),
            ),
            workspace_resources=(
                WorkspaceResourceRequest(path=parsed.path, access=access),  # type: ignore[arg-type]
            ),
        )

    return TrustedActionDefinition(tool, FileInput, resolve, executor)


def router(root: Path, *definitions: TrustedActionDefinition):
    plans = SQLiteExecutionPlanStore(root.parent / "state/plans.db")
    audit = SQLiteActionAuditStore(root.parent / "state/audit.db")
    value = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: root,
    )
    for item in definitions:
        value.register(item)
    return value, plans, audit


def invocation(tool: TrustedToolBinding, *, invocation_id: UUID | None = None):
    return CodingActionInvocation(
        invocation_id=invocation_id or uuid4(),
        source=tool.source,
        source_id=tool.source_id,
        tool=tool.tool,
        tool_version=tool.tool_version,
        tool_fingerprint=tool.tool_fingerprint,
        arguments={"path": "file.txt"},
        idempotency_key=(
            "test-write" if tool.effect_class is EffectClass.NON_IDEMPOTENT_WRITE else None
        ),
    )


@pytest.mark.parametrize("kind", ["git_ref", "external"])
def test_readonly_non_workspace_resource_is_not_misclassified_as_write(
    kind: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    tool = binding()
    planning = context(root)

    decision = DefaultCodingRiskPolicy().evaluate(
        tool,
        (
            canonical_action_resource(
                kind=kind,  # type: ignore[arg-type]
                access="read",
                identifier={"resource": "bounded"},
            ),
        ),
        planning.sandbox,
        planning.secrets,
    )

    assert decision.policy_id == "default.allow-bounded-read"


async def test_bounded_read_is_planned_executed_and_audited(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("public-output", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"text": "ok"}))
    actions, plans, audit = router(root, definition(tool, executor))

    planned = actions.plan(invocation(tool), context(root))
    outcome = await actions.execute(planned.plan.execution.plan_id)
    events = actions.events(planned.plan.execution.plan_id)

    assert planned.state == "ready"
    assert outcome.output == {"text": "ok"}
    assert executor.calls == 1
    assert [event.to_state for event in events] == ["ready", "running", "succeeded"]
    assert events[-1].output_sha256 == canonical_digest({"text": "ok"})
    payload = audit._db.execute(  # noqa: SLF001 - 验证审计正文边界
        "SELECT payload FROM action_audit_events WHERE plan_id = ? AND sequence = 3",
        (str(planned.plan.execution.plan_id),),
    ).fetchone()[0]
    assert "public-output" not in payload and '"text":"ok"' not in payload
    plans.close()
    audit.close()


async def test_write_requires_exact_approval_and_workspace_freshness(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="succeeded", output={"revision": "after"}),
        ActionExecutionOutcome(kind="succeeded", output={"revision": "after"}),
    )
    actions, plans, audit = router(root, definition(tool, executor))
    planned = actions.plan(invocation(tool), context(root))

    with pytest.raises(KernelError) as not_approved:
        await actions.execute(planned.plan.execution.plan_id)
    assert not_approved.value.code == "action_not_approved"
    approved = actions.decide(
        planned.plan.execution.plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    assert approved.state == "ready"
    target.write_text("drift", encoding="utf-8")
    with pytest.raises(KernelError) as stale:
        await actions.execute(planned.plan.execution.plan_id)
    assert stale.value.code == "execution_plan_stale"
    assert executor.calls == 0
    plans.close()
    audit.close()


async def test_running_recovery_enters_unknown_then_reconciles_once(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="lost_response"),
        ActionExecutionOutcome(kind="succeeded", output={"reconciled": True}),
    )
    actions, plans, audit = router(root, definition(tool, executor))
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    audit.transition(plan_id, expected={"ready"}, target="running", executor_id="test.file")

    assert actions.recover_interrupted() == (plan_id,)
    result = await actions.reconcile(plan_id)
    assert result.kind == "succeeded"
    assert executor.calls == 0
    assert executor.reconciliations == 1
    assert [event.to_state for event in actions.events(plan_id)] == [
        "pending_approval",
        "ready",
        "running",
        "unknown",
        "reconciling",
        "succeeded",
    ]
    plans.close()
    audit.close()


async def test_real_host_exit_recovers_to_unknown_without_replaying_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    marker = tmp_path / "external-effect.log"
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    actions, plans, audit = router(root, definition(tool, CrashExecutor(marker)))
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    plans_path = str(plans._path)  # noqa: SLF001 - 跨进程恢复测试需要复用持久文件
    audit_path = str(audit._path)  # noqa: SLF001 - 跨进程恢复测试需要复用持久文件
    plans.close()
    audit.close()

    script = (
        "from tests.trusted_actions.test_router import _action_crash_worker; "
        "import sys; _action_crash_worker(*sys.argv[1:])"
    )
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            script,
            plans_path,
            audit_path,
            str(root),
            str(plan_id),
            str(marker),
        ],
        cwd=Path(__file__).parents[2],
        check=False,
        timeout=30,
    )
    assert completed.returncode == 73

    reopened_plans = SQLiteExecutionPlanStore(plans_path)
    reopened_audit = SQLiteActionAuditStore(audit_path)
    reopened = TrustedActionRouter(
        plans=reopened_plans,
        audit=reopened_audit,
        workspace_root=lambda _: root,
    )
    reopened.register(definition(tool, CrashExecutor(marker)))
    assert reopened.recover_interrupted() == (plan_id,)
    result = await reopened.reconcile(plan_id)

    assert result.kind == "succeeded"
    assert marker.read_bytes() == b"effect\n"
    assert [event.to_state for event in reopened.events(plan_id)][-3:] == [
        "unknown",
        "reconciling",
        "succeeded",
    ]
    reopened_plans.close()
    reopened_audit.close()


@pytest.mark.parametrize("source", ["builtin", "mcp", "skill", "hook", "custom"])
def test_every_source_uses_the_same_canonical_policy(source: str, tmp_path: Path) -> None:
    root = tmp_path / source
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    source_id = "harnessix" if source == "builtin" else f"extension.{source}"
    tool = binding(source=source, source_id=source_id)
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    actions, plans, audit = router(root, definition(tool, executor))

    planned = actions.plan(invocation(tool), context(root))

    assert planned.state == "ready"
    assert planned.plan.execution.policy.policy_id == "default.allow-bounded-read"
    assert planned.plan.resources[0].kind == "workspace"
    plans.close()
    audit.close()


@pytest.mark.parametrize("secret_field", ["api_key", "API_KEY", "githubToken", "service-password"])
def test_unregistered_forged_and_secret_bearing_calls_fail_closed(
    secret_field: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    actions, plans, audit = router(root, definition(tool, executor))

    with pytest.raises(KernelError) as unknown:
        actions.plan(
            invocation(tool).model_copy(update={"tool": "extension.bypass"}), context(root)
        )
    assert unknown.value.code == "trusted_tool_not_registered"
    with pytest.raises(KernelError) as changed:
        actions.plan(invocation(tool).model_copy(update={"tool_version": "2"}), context(root))
    assert changed.value.code == "trusted_tool_contract_changed"
    with pytest.raises(KernelError) as secret:
        actions.plan(
            invocation(tool).model_copy(
                update={"arguments": {"path": "file.txt", secret_field: "x"}}
            ),
            context(root),
        )
    assert secret.value.code == "raw_secret_rejected"
    with pytest.raises(ValidationError):
        CodingActionInvocation.model_validate(
            {**invocation(tool).model_dump(), "effect_class": "read_only"}
        )
    plans.close()
    audit.close()


async def test_extension_port_has_no_privileged_objects_and_is_source_scoped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    mcp = binding(source="mcp", source_id="server.safe")
    builtin = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"ok": True}))
    actions, plans, audit = router(
        root,
        definition(mcp, executor),
        definition(builtin, executor),
    )
    port = actions.extension_port(
        source="mcp", source_id="server.safe", context=lambda: context(root)
    )

    assert not hasattr(port, "executor")
    assert not hasattr(port, "session")
    assert not hasattr(port, "secrets")
    assert [item.source_id for item in port.bindings()] == ["server.safe"]
    planned = port.plan(
        invocation_id=uuid4(),
        tool=mcp.tool,
        tool_version=mcp.tool_version,
        tool_fingerprint=mcp.tool_fingerprint,
        arguments={"path": "file.txt"},
    )
    assert (await port.execute(planned.plan.execution.plan_id)).kind == "succeeded"

    foreign = actions.plan(invocation(builtin), context(root))
    with pytest.raises(KernelError) as denied:
        port.status(foreign.plan.execution.plan_id)
    assert denied.value.code == "extension_plan_denied"
    plans.close()
    audit.close()


def test_registration_rejects_schema_substitution(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    tool = binding().model_copy(update={"input_schema_sha256": "0" * 64})
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    actions = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: root)
    with pytest.raises((KernelError, ValidationError)):
        actions.register(definition(tool, executor))
    plans.close()
    audit.close()


def test_audit_store_fails_closed_on_corrupt_index(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    actions, plans, audit = router(root, definition(tool, executor))
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    audit._db.execute(  # noqa: SLF001 - 故障注入
        "UPDATE action_route_snapshots SET sequence = 2 WHERE plan_id = ?", (str(plan_id),)
    )
    with pytest.raises(KernelError) as corrupt:
        actions.status(plan_id)
    assert corrupt.value.code == "action_audit_store_corrupt"
    plans.close()
    audit.close()


def test_audit_store_fails_closed_on_corrupt_event_chain(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    actions, plans, audit = router(root, definition(tool, executor))
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    audit._db.execute(  # noqa: SLF001 - 故障注入
        "UPDATE action_audit_events SET digest = ? WHERE plan_id = ? AND sequence = 1",
        ("0" * 64, str(plan_id)),
    )

    with pytest.raises(KernelError) as corrupt:
        actions.events(plan_id)

    assert corrupt.value.code == "action_audit_store_corrupt"
    plans.close()
    audit.close()


def test_audit_store_rejects_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    database = sqlite3.connect(path)
    database.execute(
        "CREATE TABLE action_audit_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO action_audit_metadata VALUES ('schema_version', '2')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteActionAuditStore(path)
    assert version.value.code == "action_audit_store_version"
