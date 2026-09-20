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
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

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


@dataclass
class SlowWriteExecutor:
    calls: int = 0
    reconciliations: int = 0

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.calls += 1
        FileInput.model_validate(arguments)
        await asyncio.sleep(1)
        return ActionExecutionOutcome(kind="succeeded")

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.reconciliations += 1
        FileInput.model_validate(arguments)
        return ActionExecutionOutcome(kind="succeeded", output={"reconciled": True})


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


async def test_explicit_argument_decoder_is_reused_for_execute_and_reconcile(
    tmp_path: Path,
) -> None:
    """动态公共Schema不能只在规划时生效，持久执行与恢复必须走同一解码器。"""

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("public-output", encoding="utf-8")
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"path": {"type": "string", "const": "file.txt"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    calls = 0

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        nonlocal calls
        calls += 1
        parsed = FileInput.model_validate(arguments)
        if parsed.path != "file.txt":
            raise ValueError("path不匹配")
        return parsed

    tool = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix",
        tool="file.access",
        tool_version="1",
        tool_fingerprint=canonical_digest("dynamic-file-tool"),
        input_schema_sha256=canonical_digest(schema),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        recovery_mode="durable_ledger",
        executor_id="test.file",
    )
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="lost_response"),
        ActionExecutionOutcome(kind="succeeded", output={"recovered": True}),
    )

    def resolve(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        parsed = FileInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="workspace",
                    access="write",
                    identifier={"location": "workspace", "path": parsed.path},
                ),
            ),
            workspace_resources=(WorkspaceResourceRequest(path=parsed.path, access="write"),),
        )

    actions, plans, audit = router(
        root,
        TrustedActionDefinition(
            tool,
            FileInput,
            resolve,
            executor,
            input_schema=schema,
            decode_arguments=decode,
        ),
    )
    request = invocation(tool)
    planned = actions.plan(request, context(root))
    actions.decide(
        planned.plan.execution.plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )

    first = await actions.execute(planned.plan.execution.plan_id)
    recovered = await actions.reconcile(planned.plan.execution.plan_id)

    assert first.kind == "unknown" and recovered.kind == "succeeded"
    assert calls == 3
    plans.close()
    audit.close()


async def test_exact_plan_retry_returns_current_route_without_recapturing_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"text": "ok"}))
    actions, plans, audit = router(root, definition(tool, executor))
    call = invocation(tool)
    planned = actions.plan(call, context(root))
    await actions.execute(planned.plan.execution.plan_id)
    current = actions.status(planned.plan.execution.plan_id)

    def forbid_recapture(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("幂等重试不得重新捕获Workspace")

    monkeypatch.setattr(
        "harnessix.trusted_actions.planning.capture_workspace_snapshot",
        forbid_recapture,
    )
    retried = actions.plan(call, context(root))

    assert retried == current
    assert retried.state == "succeeded"
    assert plans.load_plan(call.invocation_id) == retried.plan.execution
    plans.close()
    audit.close()


def test_plan_retry_repairs_execution_store_after_inter_store_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("before", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"text": "ok"}))
    actions, plans, audit = router(root, definition(tool, executor))
    call = invocation(tool)
    original_save = plans.save_plan
    calls = 0

    def fail_once(plan: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KernelError("injected_execution_store_failure", "故障注入")
        original_save(plan)  # type: ignore[arg-type]

    monkeypatch.setattr(plans, "save_plan", fail_once)
    with pytest.raises(KernelError) as interrupted:
        actions.plan(call, context(root))
    assert interrupted.value.code == "injected_execution_store_failure"
    durable_route = actions.status(call.invocation_id)
    with pytest.raises(KernelError) as missing:
        plans.load_plan(call.invocation_id)
    assert missing.value.code == "execution_plan_not_found"

    target.write_text("changed-after-crash", encoding="utf-8")
    repaired = actions.plan(call, context(root))

    assert repaired == durable_route
    assert plans.load_plan(call.invocation_id) == durable_route.plan.execution
    assert calls == 2
    plans.close()
    audit.close()


def test_plan_retry_rejects_reused_invocation_identity_with_other_arguments(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    (root / "other.txt").write_text("other", encoding="utf-8")
    tool = binding()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    actions, plans, audit = router(root, definition(tool, executor))
    original = invocation(tool)
    actions.plan(original, context(root))

    with pytest.raises(KernelError) as conflict:
        actions.plan(
            original.model_copy(update={"arguments": {"path": "other.txt"}}),
            context(root),
        )

    assert conflict.value.code == "action_invocation_conflict"
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


def test_approval_checkpoint_first_crash_replays_original_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    actions, plans, audit = router(
        root,
        definition(tool, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))),
    )
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    original_transition = audit.transition
    failed = False

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise KernelError("injected_audit_failure", "故障注入")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(audit, "transition", fail_once)
    decision = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer")
    with pytest.raises(KernelError) as interrupted:
        actions.decide(plan_id, decision)
    assert interrupted.value.code == "injected_audit_failure"
    checkpoint = plans.load_approval(plan_id)
    assert checkpoint is not None
    assert actions.status(plan_id).state == "pending_approval"

    recovered = actions.decide(plan_id, decision)

    assert recovered.state == "ready"
    assert plans.load_approval(plan_id) == checkpoint
    with pytest.raises(KernelError) as conflict:
        actions.decide(
            plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="reviewer"),
        )
    assert conflict.value.code == "approval_conflict"
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


def test_register_many_does_not_publish_a_valid_prefix_on_later_conflict(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output=None))
    existing = binding(source_id="existing")
    fresh = binding(source_id="fresh")
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    actions = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: root)
    actions.register(definition(existing, executor))

    with pytest.raises(KernelError) as duplicate:
        actions.register_many(
            (
                definition(fresh, executor),
                definition(existing, executor),
            )
        )

    assert duplicate.value.code == "trusted_tool_duplicate"
    assert actions.bindings() == (existing,)
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
    database.execute("INSERT INTO action_audit_metadata VALUES ('schema_version', '3')")
    database.commit()
    database.close()
    with pytest.raises(KernelError) as version:
        SQLiteActionAuditStore(path)
    assert version.value.code == "action_audit_store_version"


def test_audit_store_migrates_v1_and_requires_runtime_owner(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    database = sqlite3.connect(path)
    database.execute(
        "CREATE TABLE action_audit_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    database.execute("INSERT INTO action_audit_metadata VALUES ('schema_version', '1')")
    database.commit()
    database.close()

    audit = SQLiteActionAuditStore(path, require_runtime_owner=True)
    assert audit._db.execute(  # noqa: SLF001 - 验证向前迁移
        "SELECT value FROM action_audit_metadata WHERE key = 'schema_version'"
    ).fetchone() == ("2",)
    assert audit._db.execute(  # noqa: SLF001 - 验证恢复表已创建
        "SELECT name FROM sqlite_master WHERE name = 'action_route_operations'"
    ).fetchone() == ("action_route_operations",)

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    tool = binding()
    actions = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: root)
    actions.register(definition(tool, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))))
    with pytest.raises(KernelError) as owner:
        actions.plan(invocation(tool), context(root))
    assert owner.value.code == "action_runtime_owner_required"

    with audit.runtime_owner() as fence:
        planned = actions.plan(invocation(tool), context(root))
        assert fence.generation == 1
        assert planned.state == "ready"
    plans.close()
    audit.close()


def test_runtime_fence_rejects_stale_owner_and_competing_process(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    first = SQLiteActionAuditStore(path, require_runtime_owner=True)
    with first.runtime_owner() as old_fence:
        script = (
            "from harnessix.trusted_actions.store import SQLiteActionAuditStore; "
            "from harnessix.agent.errors import KernelError; import sys; "
            "store=SQLiteActionAuditStore(sys.argv[1], require_runtime_owner=True); "
            "\ntry:\n  with store.runtime_owner(): pass\n"
            "except KernelError as error:\n  print(error.code)\n  raise SystemExit(0)\n"
            "raise SystemExit(2)"
        )
        competed = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert competed.returncode == 0
        assert competed.stdout.strip() == "action_runtime_busy"

    second = SQLiteActionAuditStore(path, require_runtime_owner=True)
    with second.runtime_owner() as new_fence:
        assert new_fence.generation == old_fence.generation + 1
        first._runtime_fence = old_fence  # noqa: SLF001 - 模拟失锁旧宿主迟到提交
        with pytest.raises(KernelError) as stale:
            first._assert_runtime_owner()  # noqa: SLF001 - 验证持久Generation栅栏
        assert stale.value.code == "action_runtime_fence_lost"
        first._runtime_fence = None  # noqa: SLF001 - 清理故障注入
    second.close()
    first.close()


async def test_write_route_timeout_enters_unknown_and_only_reconciles(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    executor = SlowWriteExecutor()
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    actions = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: root,
        execute_timeout_seconds=0.01,
    )
    actions.register(definition(tool, executor))  # type: ignore[arg-type]
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )

    timed_out = await actions.execute(plan_id)
    recovered = await actions.reconcile(plan_id)

    assert timed_out.kind == "unknown"
    assert timed_out.error_code == "write_effect_timeout_unknown"
    assert recovered.kind == "succeeded"
    assert executor.calls == executor.reconciliations == 1
    operations = audit.operations()
    assert [(item.phase, item.state) for item in operations] == [
        ("execute", "completed"),
        ("reconcile", "completed"),
    ]
    assert operations[0].completion_code == "write_effect_timeout_unknown"
    plans.close()
    audit.close()


async def test_reconcile_attempts_are_bounded_without_reexecute(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="effect_unknown"),
        ActionExecutionOutcome(kind="unknown", error_code="still_unknown"),
    )
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    actions = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: root,
        max_reconciliation_attempts=2,
    )
    actions.register(definition(tool, executor))
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    assert (await actions.execute(plan_id)).kind == "unknown"
    assert (await actions.reconcile(plan_id)).kind == "unknown"
    assert (await actions.reconcile(plan_id)).kind == "unknown"

    exhausted = await actions.reconcile(plan_id)

    assert exhausted.kind == "manual_intervention"
    assert exhausted.error_code == "reconciliation_attempts_exhausted"
    assert executor.calls == 1
    assert executor.reconciliations == 2
    assert actions.status(plan_id).state == "manual_intervention"
    plans.close()
    audit.close()


async def test_result_then_audit_failure_recovers_without_duplicate_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    tool = binding(
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="succeeded", output={"effect": "committed"}),
        ActionExecutionOutcome(kind="succeeded", output={"reconciled": True}),
    )
    actions, plans, audit = router(root, definition(tool, executor))
    planned = actions.plan(invocation(tool), context(root))
    plan_id = planned.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    original_complete = audit.complete_operation
    failed = False

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise KernelError("injected_audit_busy", "故障注入")
        return original_complete(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(audit, "complete_operation", fail_once)
    with pytest.raises(KernelError) as interrupted:
        await actions.execute(plan_id)
    assert interrupted.value.code == "injected_audit_busy"
    assert actions.status(plan_id).state == "running"

    recovered_route = actions.recover_interrupted_plan(plan_id)
    recovered = await actions.reconcile(plan_id)

    assert recovered_route.state == "unknown"
    assert recovered.kind == "succeeded"
    assert executor.calls == executor.reconciliations == 1
    assert [item.state for item in audit.operations()] == ["interrupted", "completed"]
    plans.close()
    audit.close()
