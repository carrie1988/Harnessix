from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, utc_now
from harnessix.execution.contracts import SandboxBindingV2, canonical_digest
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.hooks import (
    HookActionInput,
    HookDefinition,
    HookMatcher,
    HookRuntime,
    SQLiteHookStore,
    build_hook_definition,
    build_hook_dispatch,
    build_hook_trust_grant,
)
from harnessix.hooks.contracts import HookRunPlan, hook_run_plan_digest
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ExtensionActionPort,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore


@dataclass
class HookExecutor:
    output: object = field(default_factory=lambda: {"decision": "allow"})
    delay: float = 0
    calls: int = 0
    inputs: list[HookActionInput] = field(default_factory=list)
    entered: asyncio.Event | None = None

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        del plan
        self.calls += 1
        parsed = HookActionInput.model_validate(arguments)
        self.inputs.append(parsed)
        if self.entered is not None:
            self.entered.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        return ActionExecutionOutcome(kind="succeeded", output=self.output)  # type: ignore[arg-type]

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        del plan, arguments
        return ActionExecutionOutcome(
            kind="manual_intervention",
            error_code="hook_reconciliation_not_supported",
        )


def planning_context(root: Path) -> ActionPlanningContext:
    capabilities = build_capability_evidence_v2(
        platform="windows" if os.name == "nt" else "posix",
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest("hook-test-provider"),
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest("hook-test-profile"),
    )
    return ActionPlanningContext(
        workspace_root=root,
        sandbox=sandbox,
        capabilities=capabilities,
    )


def hook_binding(source_id: str, tool: str) -> TrustedToolBinding:
    return build_trusted_tool_binding(
        source="hook",
        source_id=source_id,
        tool=tool,
        tool_version="1",
        tool_fingerprint=canonical_digest({"source": source_id, "tool": tool}),
        input_schema_sha256=canonical_digest(HookActionInput.model_json_schema()),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        recovery_mode="none",
        executor_id=f"hook.{tool.replace('.', '-')}",
    )


def hook_action(
    binding: TrustedToolBinding,
    executor: HookExecutor,
    *,
    secret_resource: bool = False,
) -> TrustedActionDefinition:
    def resolve(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        parsed = HookActionInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="secret" if secret_resource else "external",
                    access="use" if secret_resource else "read",
                    identifier={
                        "hook": parsed.hook_id,
                        "dispatch": str(parsed.dispatch_id),
                    },
                ),
            )
        )

    return TrustedActionDefinition(binding, HookActionInput, resolve, executor)


def definition(
    binding: TrustedToolBinding,
    *,
    hook_id: str,
    source_kind: str = "bundled",
    event: str = "before_action",
    order: int = 0,
    timeout_ms: int = 1000,
    matcher: HookMatcher | None = None,
) -> HookDefinition:
    if matcher is None and event in {"before_action", "after_action"}:
        matcher = HookMatcher(action_source="*", action_tool="*")
    return build_hook_definition(
        source_id=binding.source_id,
        source_kind=source_kind,  # type: ignore[arg-type]
        source_version="1",
        hook_id=hook_id,
        event=event,  # type: ignore[arg-type]
        matcher=matcher,
        order=order,
        mode="blocking" if event == "before_action" else "advisory",
        failure_policy="fail_closed" if event == "before_action" else "record_only",
        timeout_ms=timeout_ms,
        action_tool=binding.tool,
        action_tool_version=binding.tool_version,
        action_tool_fingerprint=binding.tool_fingerprint,
    )


def action_runtime(
    tmp_path: Path,
    actions: tuple[tuple[TrustedToolBinding, HookExecutor], ...],
) -> tuple[
    TrustedActionRouter,
    dict[str, ExtensionActionPort],
    SQLiteExecutionPlanStore,
    SQLiteActionAuditStore,
]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    for binding, executor in actions:
        router.register(hook_action(binding, executor))
    ports = {
        source_id: ExtensionActionPort(
            router,
            source="hook",
            source_id=source_id,
            context=lambda: planning_context(workspace),
        )
        for source_id in {binding.source_id for binding, _ in actions}
    }
    return router, ports, plans, audit


def before_dispatch(*, tool: str = "file.read", dispatch_id: UUID | None = None):
    return build_hook_dispatch(
        dispatch_id=dispatch_id or uuid4(),
        event="before_action",
        thread_id=uuid4(),
        turn_id=uuid4(),
        target_action_source="builtin",
        target_action_source_id="harnessix",
        target_action_tool=tool,
        target_action_plan_id=uuid4(),
        arguments_sha256=canonical_digest({"path": "README.md"}),
        occurred_at=utc_now(),
    )


async def test_blocking_hook_allows_and_replays_same_dispatch_once(tmp_path: Path) -> None:
    binding = hook_binding("policy", "check.allow")
    executor = HookExecutor()
    router, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    hooks = SQLiteHookStore(tmp_path / "state/hooks.db")
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="allow"),),
        trust_grants=(),
        ports=ports,
        store=hooks,
    )
    dispatch = before_dispatch()

    first = await runtime.dispatch(dispatch)
    second = await runtime.dispatch(dispatch)

    assert first == second
    assert first.allowed and first.runs[0].state == "succeeded"
    assert executor.calls == 1
    assert [event.to_state for event in hooks.events(first.runs[0].plan.run_id)] == [
        "ready",
        "running",
        "succeeded",
    ]
    assert router.status(first.runs[0].plan.action_plan_id).state == "succeeded"
    plans.close()
    audit.close()


async def test_denial_stops_later_blocking_hook_in_deterministic_order(tmp_path: Path) -> None:
    first_binding = hook_binding("policy", "check.deny")
    second_binding = hook_binding("policy", "check.later")
    first_executor = HookExecutor({"decision": "deny", "reason_code": "protected_branch"})
    second_executor = HookExecutor()
    _, ports, plans, audit = action_runtime(
        tmp_path,
        ((first_binding, first_executor), (second_binding, second_executor)),
    )
    runtime = HookRuntime(
        registry_id="default",
        definitions=(
            definition(second_binding, hook_id="later", order=20),
            definition(first_binding, hook_id="deny", order=10),
        ),
        trust_grants=(),
        ports=ports,
        store=SQLiteHookStore(tmp_path / "state/hooks.db"),
    )

    result = await runtime.dispatch(before_dispatch())

    assert not result.allowed
    assert [run.plan.definition.hook_id for run in result.runs] == ["deny"]
    assert result.runs[0].state == "blocked"
    assert first_executor.calls == 1 and second_executor.calls == 0
    plans.close()
    audit.close()


def test_non_bundled_hook_requires_exact_unexpired_definition_grant(tmp_path: Path) -> None:
    binding = hook_binding("workspace", "check.workspace")
    executor = HookExecutor()
    _, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    selected = definition(binding, hook_id="workspace", source_kind="workspace")
    now = utc_now()
    grant = build_hook_trust_grant(
        selected,
        granted_by="administrator",
        granted_at=now,
        expires_at=now + timedelta(hours=1),
    )
    store = SQLiteHookStore(tmp_path / "state/hooks.db")

    with pytest.raises(KernelError) as missing:
        HookRuntime(
            registry_id="missing",
            definitions=(selected,),
            trust_grants=(),
            ports=ports,
            store=store,
            captured_at=now,
        )
    assert missing.value.code == "hook_trust_required"

    changed = definition(
        binding,
        hook_id="workspace",
        source_kind="workspace",
        timeout_ms=2000,
    )
    with pytest.raises(KernelError) as stale:
        HookRuntime(
            registry_id="stale",
            definitions=(changed,),
            trust_grants=(grant,),
            ports=ports,
            store=store,
            captured_at=now,
        )
    assert stale.value.code == "hook_trust_required"

    expired_grant = build_hook_trust_grant(
        selected,
        granted_by="administrator",
        granted_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    with pytest.raises(KernelError) as expired:
        HookRuntime(
            registry_id="expired",
            definitions=(selected,),
            trust_grants=(expired_grant,),
            ports=ports,
            store=store,
            captured_at=now,
        )
    assert expired.value.code == "hook_trust_required"

    runtime = HookRuntime(
        registry_id="trusted",
        definitions=(selected,),
        trust_grants=(grant,),
        ports=ports,
        store=store,
        captured_at=now,
    )
    assert runtime.registry.trust_grant_sha256 == (grant.grant_sha256,)
    plans.close()
    audit.close()


def test_before_action_contract_rejects_advisory_or_regex_matcher() -> None:
    with pytest.raises(ValidationError):
        build_hook_definition(
            source_id="policy",
            source_kind="bundled",
            source_version="1",
            hook_id="invalid",
            event="before_action",
            matcher=HookMatcher(action_source="*", action_tool="*"),
            order=0,
            mode="advisory",
            failure_policy="record_only",
            timeout_ms=1000,
            action_tool="check.allow",
            action_tool_version="1",
            action_tool_fingerprint="0" * 64,
        )
    with pytest.raises(ValidationError):
        HookMatcher(action_source="builtin", action_tool="^(shell|git)$")


def test_hook_registry_rejects_non_readonly_or_wrong_schema_binding(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    unsafe = build_trusted_tool_binding(
        source="hook",
        source_id="policy",
        tool="check.unsafe",
        tool_version="1",
        tool_fingerprint=canonical_digest("unsafe"),
        input_schema_sha256=canonical_digest(HookActionInput.model_json_schema()),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        recovery_mode="durable_ledger",
        executor_id="hook.unsafe",
    )
    router.register(hook_action(unsafe, HookExecutor()))
    port = ExtensionActionPort(
        router,
        source="hook",
        source_id="policy",
        context=lambda: planning_context(workspace),
    )

    with pytest.raises(KernelError) as caught:
        HookRuntime(
            registry_id="default",
            definitions=(definition(unsafe, hook_id="unsafe"),),
            trust_grants=(),
            ports={"policy": port},
            store=SQLiteHookStore(tmp_path / "state/hooks.db"),
        )
    assert caught.value.code == "hook_action_binding_unsafe"
    plans.close()
    audit.close()


async def test_hook_action_requiring_secret_or_approval_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    binding = hook_binding("policy", "check.secret-resource")
    executor = HookExecutor()
    router.register(hook_action(binding, executor, secret_resource=True))
    port = ExtensionActionPort(
        router,
        source="hook",
        source_id="policy",
        context=lambda: planning_context(workspace),
    )
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="secret-resource"),),
        trust_grants=(),
        ports={"policy": port},
        store=SQLiteHookStore(tmp_path / "state/hooks.db"),
    )

    result = await runtime.dispatch(before_dispatch())

    assert not result.allowed
    assert result.runs[0].error_code == "hook_action_not_ready"
    assert executor.calls == 0
    assert router.status(result.runs[0].plan.action_plan_id).state == "denied"
    plans.close()
    audit.close()


async def test_allow_hook_cannot_override_target_action_policy(tmp_path: Path) -> None:
    binding = hook_binding("policy", "check.allow")
    executor = HookExecutor()
    router, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="allow"),),
        trust_grants=(),
        ports=ports,
        store=SQLiteHookStore(tmp_path / "state/hooks.db"),
    )
    hook_result = await runtime.dispatch(before_dispatch(tool="process.destroy"))

    class EmptyInput(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    destructive = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix",
        tool="process.destroy",
        tool_version="1",
        tool_fingerprint=canonical_digest("destroy"),
        input_schema_sha256=canonical_digest(EmptyInput.model_json_schema()),
        effect_class=EffectClass.DESTRUCTIVE,
        risk_level=RiskLevel.CRITICAL,
        recovery_mode="durable_ledger",
        executor_id="test.destroy",
    )

    class NeverExecutor:
        async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
            raise AssertionError("denied action must not execute")

        async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
            raise AssertionError("denied action must not reconcile")

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        del arguments, context
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="process",
                    access="execute",
                    identifier="destroy",
                ),
            )
        )

    router.register(TrustedActionDefinition(destructive, EmptyInput, resolve, NeverExecutor()))
    from harnessix.trusted_actions.contracts import CodingActionInvocation

    target = router.plan(
        CodingActionInvocation(
            source="builtin",
            source_id="harnessix",
            tool=destructive.tool,
            tool_version=destructive.tool_version,
            tool_fingerprint=destructive.tool_fingerprint,
            arguments={},
            idempotency_key="destroy-once",
        ),
        planning_context(tmp_path / "workspace"),
    )

    assert hook_result.allowed
    assert target.state == "denied"
    plans.close()
    audit.close()


async def test_hook_timeout_cancels_and_persists_underlying_action(tmp_path: Path) -> None:
    binding = hook_binding("policy", "check.slow")
    executor = HookExecutor(delay=10)
    router, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    store = SQLiteHookStore(tmp_path / "state/hooks.db")
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="slow", timeout_ms=100),),
        trust_grants=(),
        ports=ports,
        store=store,
    )

    result = await runtime.dispatch(before_dispatch())
    run = result.runs[0]

    assert not result.allowed
    assert run.state == "failed" and run.error_code == "hook_timeout"
    action = router.status(run.plan.action_plan_id)
    assert action.state == "failed"
    assert router.events(run.plan.action_plan_id)[-1].error_code == "executor_cancelled"
    plans.close()
    audit.close()


async def test_outer_cancellation_persists_hook_and_action_cancellation(tmp_path: Path) -> None:
    entered = asyncio.Event()
    binding = hook_binding("policy", "check.cancel")
    executor = HookExecutor(delay=10, entered=entered)
    router, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    store = SQLiteHookStore(tmp_path / "state/hooks.db")
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="cancel"),),
        trust_grants=(),
        ports=ports,
        store=store,
    )
    task = asyncio.create_task(runtime.dispatch(before_dispatch()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    row = store._db.execute("SELECT run_id FROM hook_run_snapshots").fetchone()  # noqa: SLF001
    run = store.load(UUID(row[0]))
    assert run.state == "cancelled" and run.error_code == "hook_cancelled"
    assert router.status(run.plan.action_plan_id).state == "failed"
    plans.close()
    audit.close()


async def test_advisory_failure_is_recorded_without_blocking(tmp_path: Path) -> None:
    binding = hook_binding("telemetry", "observe.turn")
    executor = HookExecutor(output={"decision": "deny", "reason_code": "not_allowed"})
    _, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    runtime = HookRuntime(
        registry_id="default",
        definitions=(
            definition(
                binding,
                hook_id="observe",
                event="turn_completed",
            ),
        ),
        trust_grants=(),
        ports=ports,
        store=SQLiteHookStore(tmp_path / "state/hooks.db"),
    )
    dispatch = build_hook_dispatch(
        dispatch_id=uuid4(),
        event="turn_completed",
        thread_id=uuid4(),
        turn_id=uuid4(),
        occurred_at=utc_now(),
    )

    result = await runtime.dispatch(dispatch)

    assert result.allowed
    assert result.runs[0].state == "failed"
    assert result.runs[0].error_code == "hook_output_invalid"
    plans.close()
    audit.close()


async def test_hook_receives_only_digests_and_secret_output_is_fail_closed(tmp_path: Path) -> None:
    canary = b"HOOK-CANARY-ae811f"
    binding = hook_binding("policy", "check.secret")
    executor = HookExecutor(output={"decision": "allow", "leak": canary.decode()})
    _, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    store = SQLiteHookStore(tmp_path / "state/hooks.db")
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="secret"),),
        trust_grants=(),
        ports=ports,
        store=store,
        protected_secret_values=(canary,),
    )

    result = await runtime.dispatch(before_dispatch())

    assert not result.allowed and result.runs[0].error_code == "hook_output_invalid"
    assert executor.inputs[0].target_action_source_id_sha256 == canonical_digest("harnessix")
    assert "README.md" not in executor.inputs[0].model_dump_json()
    assert canary not in (tmp_path / "state/hooks.db").read_bytes()
    plans.close()
    audit.close()


def test_matcher_filters_exact_source_and_tool(tmp_path: Path) -> None:
    binding = hook_binding("policy", "check.git")
    executor = HookExecutor()
    _, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    runtime = HookRuntime(
        registry_id="default",
        definitions=(
            definition(
                binding,
                hook_id="git",
                matcher=HookMatcher(action_source="builtin", action_tool="git.push"),
            ),
        ),
        trust_grants=(),
        ports=ports,
        store=SQLiteHookStore(tmp_path / "state/hooks.db"),
    )

    result = asyncio.run(runtime.dispatch(before_dispatch(tool="file.read")))

    assert result.allowed and result.runs == ()
    assert executor.calls == 0
    plans.close()
    audit.close()


def test_store_recovers_interrupted_run_without_replay(tmp_path: Path) -> None:
    binding = hook_binding("policy", "check.allow")
    executor = HookExecutor()
    _, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    store = SQLiteHookStore(tmp_path / "state/hooks.db")
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="allow"),),
        trust_grants=(),
        ports=ports,
        store=store,
    )
    dispatch = before_dispatch()
    selected = runtime.registry.definitions[0]
    run_id = uuid4()
    candidate = HookRunPlan.model_construct(
        _fields_set=None,
        run_id=run_id,
        registry_id=runtime.registry.registry_id,
        registry_generation=runtime.registry.generation,
        registry_sha256=runtime.registry.registry_sha256,
        definition=selected,
        dispatch=dispatch,
        action_plan_id=run_id,
        input_sha256=canonical_digest("input"),
        plan_sha256="0" * 64,
    )
    plan = HookRunPlan(
        **candidate.model_dump(exclude={"plan_sha256"}),
        plan_sha256=hook_run_plan_digest(candidate),
    )
    store.begin(plan)
    store.close()

    worker = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("crash_worker.py")),
            str(tmp_path / "state/hooks.db"),
            str(run_id),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert worker.returncode == 73, worker.stderr

    reopened = SQLiteHookStore(tmp_path / "state/hooks.db")
    assert reopened.recover_interrupted() == (run_id,)
    assert reopened.load(run_id).state == "interrupted"
    assert reopened.recover_interrupted() == ()
    plans.close()
    audit.close()


def test_hook_store_detects_event_chain_corruption(tmp_path: Path) -> None:
    binding = hook_binding("policy", "check.allow")
    executor = HookExecutor()
    _, ports, plans, audit = action_runtime(tmp_path, ((binding, executor),))
    store = SQLiteHookStore(tmp_path / "state/hooks.db")
    runtime = HookRuntime(
        registry_id="default",
        definitions=(definition(binding, hook_id="allow"),),
        trust_grants=(),
        ports=ports,
        store=store,
    )
    result = asyncio.run(runtime.dispatch(before_dispatch()))
    run_id = result.runs[0].plan.run_id
    store.close()
    database = sqlite3.connect(tmp_path / "state/hooks.db")
    database.execute(
        "UPDATE hook_run_events SET digest = ? WHERE run_id = ? AND sequence = 2",
        ("f" * 64, str(run_id)),
    )
    database.commit()
    database.close()

    corrupt = SQLiteHookStore(tmp_path / "state/hooks.db")
    with pytest.raises(KernelError) as caught:
        corrupt.events(run_id)
    assert caught.value.code == "hook_store_corrupt"
    plans.close()
    audit.close()
