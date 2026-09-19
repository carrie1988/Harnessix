from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Thread,
    ToolCallContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
    utc_now,
)
from harnessix.execution.contracts import SandboxBindingV2, canonical_digest
from harnessix.execution.planner import build_capability_evidence_v2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRouteSnapshot,
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
from harnessix.workspace.contracts import WorkspaceResourceRequest


class FileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=256)


@dataclass
class FakeExecutor:
    outcome: ActionExecutionOutcome
    reconciled: ActionExecutionOutcome | None = None
    calls: int = 0
    reconciliations: int = 0

    async def execute(self, _plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        FileInput.model_validate(arguments)
        self.calls += 1
        return self.outcome

    async def reconcile(self, _plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        FileInput.model_validate(arguments)
        self.reconciliations += 1
        assert self.reconciled is not None
        return self.reconciled


@dataclass
class FixedReview:
    artifact: ArtifactRef | None
    calls: int = 0

    async def review(self, *_args: object) -> TrustedActionReview:
        self.calls += 1
        return TrustedActionReview(diff_artifact=self.artifact)


@dataclass
class FixedOutput:
    projected: JsonValue
    calls: int = 0
    output_sha256: str | None = None
    artifact_sha256: str | None = None

    async def output(
        self,
        _route: ActionRouteSnapshot,
        _thread: Thread,
        _turn: Turn,
        _call: ToolCallContent,
        *,
        expected_output_sha256: str,
        expected_artifact_sha256: str,
        cancel: CancelToken,
    ) -> JsonValue:
        cancel.checkpoint()
        self.calls += 1
        self.output_sha256 = expected_output_sha256
        self.artifact_sha256 = expected_artifact_sha256
        return self.projected


def descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name="workspace.patch",
        version="1",
        description="事务修改Workspace内一个有界文本文件",
        input_schema=FileInput.model_json_schema(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
    )


def runtime_context(root: Path) -> ActionPlanningContext:
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
    return ActionPlanningContext(
        workspace_root=root,
        sandbox=SandboxBindingV2(
            level="host_guarded",
            backend="host",
            backend_version="1",
            network="none",
            capability_digest=capabilities.evidence_digest,
            profile_digest=canonical_digest("test-profile"),
        ),
        capabilities=capabilities,
    )


def build_gateway(
    root: Path,
    executor: FakeExecutor,
    *,
    presentation: str = "tool",
    review: FixedReview | None = None,
    output: FixedOutput | None = None,
) -> tuple[
    RouterBackedAgentActionGateway,
    TrustedActionRouter,
    SQLiteExecutionPlanStore,
    SQLiteActionAuditStore,
]:
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
                    identifier={"location": "workspace", "path": checked.path},
                ),
            ),
            workspace_resources=(WorkspaceResourceRequest(path=checked.path, access="write"),),
        )

    plans = SQLiteExecutionPlanStore(root.parent / "state/plans.db")
    audit = SQLiteActionAuditStore(root.parent / "state/audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: root,
    )
    router.register(TrustedActionDefinition(binding, FileInput, resolve, executor))
    gateway = RouterBackedAgentActionGateway(
        router,
        (tool,),
        lambda *_: runtime_context(root),
        presentations={tool.name: presentation},  # type: ignore[dict-item]
        reviews=review,
        outputs={tool.name: output} if output is not None else None,
    )
    return gateway, router, plans, audit


def agent_state(root: Path) -> tuple[Thread, Turn, ToolCallContent]:
    now = utc_now()
    turn = Turn(
        turn_id=uuid4(),
        request_id="request",
        request_fingerprint="1" * 64,
        status=TurnStatus.EXECUTING_TOOLS,
        budget=Budget(),
        created_at=now,
    )
    thread = Thread(
        thread_id=uuid4(),
        workspace=str(root),
        active_turn_id=turn.turn_id,
        turns=(turn,),
        created_at=now,
        updated_at=now,
    )
    tool = descriptor()
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="provider-call",
        tool=tool.name,
        tool_version=tool.version,
        effect_class=tool.effect_class,
        arguments={"path": "file.txt"},
        requires_approval=True,
        tool_fingerprint=tool_fingerprint(tool),
    )
    return thread, turn, call


def close_stores(
    gateway: RouterBackedAgentActionGateway,
    plans: SQLiteExecutionPlanStore,
    audit: SQLiteActionAuditStore,
) -> None:
    gateway.close()
    plans.close()
    audit.close()


def test_gateway_rejects_ambiguous_or_unknown_review_binding(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    gateway, router, plans, audit = build_gateway(
        root,
        FakeExecutor(ActionExecutionOutcome(kind="succeeded")),
    )
    review = FixedReview(None)
    second = descriptor().model_copy(update={"name": "process.run"})

    with pytest.raises(KernelError, match="多Tool Gateway必须显式按Tool绑定Review") as ambiguous:
        RouterBackedAgentActionGateway(
            router,
            (descriptor(), second),
            lambda *_: runtime_context(root),
            reviews=review,
        )
    assert ambiguous.value.code == "trusted_action_gateway_invalid"

    with pytest.raises(KernelError, match="Gateway Review包含未知Tool") as unknown:
        RouterBackedAgentActionGateway(
            router,
            (descriptor(),),
            lambda *_: runtime_context(root),
            reviews={"unknown": review},
        )
    assert unknown.value.code == "trusted_action_gateway_invalid"
    close_stores(gateway, plans, audit)


async def test_prepare_is_deterministic_and_patch_requires_same_review_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    artifact = ArtifactRef(
        artifact_id=uuid4(),
        sha256="a" * 64,
        size_bytes=10,
        records=1,
        complete=True,
        expires_at=utc_now() + timedelta(hours=1),
    )
    review = FixedReview(artifact)
    gateway, router, plans, audit = build_gateway(
        root,
        FakeExecutor(ActionExecutionOutcome(kind="succeeded")),
        presentation="patch_batch",
        review=review,
    )
    thread, turn, call = agent_state(root)

    first = await gateway.prepare(thread, turn, call, CancelToken())
    second = await gateway.prepare(thread, turn, call, CancelToken())

    assert isinstance(first, TrustedActionApprovalRequestContent)
    assert second == first
    assert first.presentation == "patch_batch" and first.diff_artifact == artifact
    assert first.plan_id == router.status(first.plan_id).plan.execution.plan_id
    assert first.plan_fingerprint == router.status(first.plan_id).plan.fingerprint
    assert len(router.events(first.plan_id)) == 1
    close_stores(gateway, plans, audit)


async def test_router_decision_is_authoritative_and_execution_projects_bounded_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"summary": "changed"}))
    gateway, router, plans, audit = build_gateway(root, executor)
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)

    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    result = await gateway.execute(thread, turn, call, approved, CancelToken())

    checkpoint = router.approval(prepared.plan_id)
    assert checkpoint is not None
    assert checkpoint.decision.request_fingerprint == prepared.execution_fingerprint
    assert approved.decision is not None
    assert approved.decision.request_fingerprint == prepared.request_fingerprint
    assert approved.decision.decided_at == checkpoint.decision.decided_at
    assert result.outcome == "succeeded" and result.output == {"summary": "changed"}
    assert result.action_id == prepared.plan_id
    assert result.trusted_action is not None
    assert result.trusted_action.plan_fingerprint == prepared.plan_fingerprint
    assert executor.calls == 1
    close_stores(gateway, plans, audit)


async def test_configured_output_provider_projects_audited_terminal_body(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    summary: dict[str, JsonValue] = {"summary": "changed"}
    artifact_sha256 = "b" * 64
    output = FixedOutput({**summary, "artifact": {"sha256": artifact_sha256}})
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded",
            output=summary,
            artifact_sha256=artifact_sha256,
        )
    )
    gateway, router, plans, audit = build_gateway(
        root,
        executor,
        presentation="process",
        output=output,
    )
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )

    result = await gateway.execute(thread, turn, call, approved, CancelToken())

    assert result.output == output.projected
    assert output.calls == 1
    assert output.output_sha256 == canonical_digest(summary)
    assert output.artifact_sha256 == artifact_sha256
    assert router.status(prepared.plan_id).state == "succeeded"
    close_stores(gateway, plans, audit)


async def test_terminal_recovery_reconstructs_output_without_reexecution(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    summary: dict[str, JsonValue] = {"summary": "persisted"}
    artifact_sha256 = "c" * 64
    output = FixedOutput({**summary, "artifact": {"sha256": artifact_sha256}})
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded",
            output=summary,
            artifact_sha256=artifact_sha256,
        )
    )
    gateway, router, plans, audit = build_gateway(
        root,
        executor,
        presentation="process",
        output=output,
    )
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    await router.execute(prepared.plan_id)

    recovered = await gateway.recover(thread, turn, call, approved, CancelToken())

    assert recovered is not None and recovered.output == output.projected
    assert executor.calls == 1 and executor.reconciliations == 0
    assert output.calls == 1
    close_stores(gateway, plans, audit)


async def test_router_first_crash_window_syncs_same_session_decision(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    gateway, router, plans, audit = build_gateway(
        root, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    )
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    decided_at = utc_now()
    router.decide(
        prepared.plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
        decided_at=decided_at,
    )

    synchronized = gateway.sync_decision(thread, turn, call, prepared)

    assert synchronized is not None and synchronized.decision is not None
    assert synchronized.route_state == "ready"
    assert synchronized.decision.decided_at == decided_at
    assert gateway.sync_decision(thread, turn, call, synchronized) == synchronized
    close_stores(gateway, plans, audit)


async def test_session_first_crash_window_fills_router_before_execute(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, router, plans, audit = build_gateway(root, executor)
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    session_decision = prepared.model_copy(
        update={
            "route_state": "ready",
            "decision": ApprovalRecord(
                outcome=ApprovalOutcome.APPROVED,
                actor="reviewer",
                request_fingerprint=prepared.request_fingerprint,
                decided_at=utc_now(),
            ),
        }
    )
    session_decision = TrustedActionApprovalRequestContent.model_validate(
        session_decision.model_dump()
    )

    result = await gateway.execute(thread, turn, call, session_decision, CancelToken())

    assert result.outcome == "succeeded" and executor.calls == 1
    assert router.status(prepared.plan_id).state == "succeeded"
    assert router.approval(prepared.plan_id) is not None
    close_stores(gateway, plans, audit)


async def test_terminal_route_recovery_only_reprojects_original_execution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, router, plans, audit = build_gateway(root, executor)
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    executed = await gateway.execute(thread, turn, call, approved, CancelToken())
    replayed = await gateway.execute(thread, turn, call, approved, CancelToken())
    recovered = await gateway.recover(thread, turn, call, approved, CancelToken())

    assert executed.outcome == "succeeded"
    assert replayed.trusted_action is not None
    assert replayed.trusted_action.origin == "execution"
    assert recovered is not None and recovered.outcome == "succeeded"
    assert recovered.trusted_action is not None
    assert recovered.trusted_action.origin == "execution"
    assert executor.calls == 1 and executor.reconciliations == 0
    assert router.status(prepared.plan_id).state == "succeeded"
    close_stores(gateway, plans, audit)


async def test_running_route_recovers_to_unknown_and_reconciles_without_reexecute(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="lost_response"),
        ActionExecutionOutcome(kind="succeeded", artifact_sha256="b" * 64),
    )
    gateway, router, plans, audit = build_gateway(root, executor)
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    audit.transition(
        prepared.plan_id,
        expected={"ready"},
        target="running",
        executor_id="product.workspace-patch",
    )

    recovered = await gateway.recover(thread, turn, call, approved, CancelToken())

    assert recovered is not None and recovered.outcome == "succeeded"
    assert recovered.trusted_action is not None
    assert recovered.trusted_action.origin == "recovery"
    assert recovered.trusted_action.artifact_sha256 == "b" * 64
    assert executor.calls == 0 and executor.reconciliations == 1
    assert [event.to_state for event in router.events(prepared.plan_id)][-3:] == [
        "unknown",
        "reconciling",
        "succeeded",
    ]
    close_stores(gateway, plans, audit)


async def test_cancellation_after_router_claim_becomes_unknown_then_reconciles(
    tmp_path: Path,
) -> None:
    class BlockingExecutor(FakeExecutor):
        started: asyncio.Event

        async def execute(self, _plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
            FileInput.model_validate(arguments)
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = BlockingExecutor(
        ActionExecutionOutcome(kind="succeeded"),
        ActionExecutionOutcome(kind="succeeded"),
    )
    executor.started = asyncio.Event()
    gateway, router, plans, audit = build_gateway(root, executor)
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )
    cancel = CancelToken()
    execution = asyncio.create_task(gateway.execute(thread, turn, call, approved, cancel))
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    assert router.status(prepared.plan_id).state == "running"
    cancel.cancel()

    with pytest.raises(TurnCancelled):
        await execution
    assert router.status(prepared.plan_id).state == "unknown"
    recovered = await gateway.recover(thread, turn, call, approved, CancelToken())

    assert recovered is not None and recovered.outcome == "succeeded"
    assert executor.calls == 1 and executor.reconciliations == 1
    close_stores(gateway, plans, audit)


async def test_conflicting_replayed_decision_and_call_drift_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    gateway, _, plans, audit = build_gateway(
        root, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    )
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)
    approved = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
    )

    with pytest.raises(KernelError) as conflict:
        gateway.decide(
            thread,
            turn,
            call,
            prepared,
            ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="reviewer"),
        )
    assert conflict.value.code == "approval_conflict"
    with pytest.raises(KernelError) as mismatch:
        await gateway.execute(
            thread,
            turn,
            call.model_copy(update={"arguments": {"path": "other.txt"}}),
            approved,
            CancelToken(),
        )
    assert mismatch.value.code == "approval_mismatch"
    close_stores(gateway, plans, audit)


async def test_gateway_rejects_missing_patch_review(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    gateway, _, plans, audit = build_gateway(
        root,
        FakeExecutor(ActionExecutionOutcome(kind="succeeded")),
        presentation="patch_batch",
    )
    thread, turn, call = agent_state(root)

    with pytest.raises(KernelError) as missing:
        await gateway.prepare(thread, turn, call, CancelToken())
    assert missing.value.code == "trusted_action_review_missing"
    close_stores(gateway, plans, audit)


async def test_gateway_rejects_descriptor_drift_from_router_binding(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, router, plans, audit = build_gateway(root, executor)
    gateway.close()
    drifted = descriptor().model_copy(update={"description": "未注册的合同变更"})

    with pytest.raises(KernelError) as mismatch:
        RouterBackedAgentActionGateway(
            router,
            (drifted,),
            lambda *_: runtime_context(root),
        )

    assert mismatch.value.code == "trusted_action_gateway_mismatch"
    plans.close()
    audit.close()


async def test_rejected_action_never_reaches_executor(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    gateway, router, plans, audit = build_gateway(root, executor)
    thread, turn, call = agent_state(root)
    prepared = await gateway.prepare(thread, turn, call, CancelToken())
    assert isinstance(prepared, TrustedActionApprovalRequestContent)

    rejected = gateway.decide(
        thread,
        turn,
        call,
        prepared,
        ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="reviewer"),
    )
    result = await gateway.execute(thread, turn, call, rejected, CancelToken())

    assert result.outcome == "failed"
    assert result.trusted_action is not None and result.trusted_action.state == "failed"
    assert router.status(prepared.plan_id).state == "denied"
    assert executor.calls == 0
    close_stores(gateway, plans, audit)
