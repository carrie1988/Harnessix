from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_runtime import (
    _recover_product_actions,
    _verify_active_product_bindings,
)
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


def _router(
    tmp_path: Path,
    executor: FakeExecutor,
) -> tuple[TrustedActionRouter, SQLiteExecutionPlanStore, SQLiteActionAuditStore]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    plans = SQLiteExecutionPlanStore(tmp_path / "state" / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state" / "audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    tool = binding(
        source_id="harnessix.product",
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )
    router.register(definition(tool, executor))
    return router, plans, audit


def _running(
    router: TrustedActionRouter,
    audit: SQLiteActionAuditStore,
    workspace: Path,
):
    tool = router.bindings(source="builtin", source_id="harnessix.product")[0]
    route = router.plan(invocation(tool), context(workspace))
    plan_id = route.plan.execution.plan_id
    router.decide(
        plan_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="operator"),
    )
    audit.transition(
        plan_id,
        expected={"ready"},
        target="running",
        executor_id=tool.executor_id,
    )
    return plan_id


async def test_startup_recovery_reconciles_without_reexecuting(tmp_path: Path) -> None:
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="not_called"),
        ActionExecutionOutcome(kind="succeeded", output={"recovered": True}),
    )
    router, plans, audit = _router(tmp_path, executor)
    plan_id = _running(router, audit, tmp_path / "workspace")
    digest = canonical_digest("action-config")

    report = await _recover_product_actions(
        router,
        audit,
        candidate_config_sha256=digest,
        recovery_config_sha256=digest,
    )

    assert report.scanned_routes == report.interrupted_routes == 1
    assert report.reconciled_routes == report.succeeded_routes == 1
    assert report.unresolved_routes == 0
    assert executor.calls == 0 and executor.reconciliations == 1
    assert router.status(plan_id).state == "succeeded"
    assert [item.to_state for item in router.events(plan_id)][-3:] == [
        "unknown",
        "reconciling",
        "succeeded",
    ]
    plans.close()
    audit.close()


async def test_startup_recovery_unknown_fails_closed_without_retry(tmp_path: Path) -> None:
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="not_called"),
        ActionExecutionOutcome(kind="unknown", error_code="still_unknown"),
    )
    router, plans, audit = _router(tmp_path, executor)
    plan_id = _running(router, audit, tmp_path / "workspace")
    digest = canonical_digest("action-config")

    with pytest.raises(KernelError) as incomplete:
        await _recover_product_actions(
            router,
            audit,
            candidate_config_sha256=digest,
            recovery_config_sha256=digest,
        )

    assert incomplete.value.code == "product_action_recovery_incomplete"
    assert router.status(plan_id).state == "unknown"
    assert executor.calls == 0 and executor.reconciliations == 1
    plans.close()
    audit.close()


async def test_startup_recovery_requires_exact_old_binding_before_transition(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="not_called"),
        ActionExecutionOutcome(kind="succeeded", output={"recovered": True}),
    )
    router, plans, audit = _router(tmp_path, executor)
    plan_id = _running(router, audit, tmp_path / "workspace")
    unregistered = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: tmp_path / "workspace",
    )

    with pytest.raises(KernelError) as missing:
        await _recover_product_actions(
            unregistered,
            audit,
            candidate_config_sha256=canonical_digest("candidate"),
            recovery_config_sha256=canonical_digest("previous"),
        )

    assert missing.value.code == "product_action_recovery_binding_unavailable"
    assert unregistered.status(plan_id).state == "running"
    assert executor.calls == executor.reconciliations == 0
    plans.close()
    audit.close()


async def test_candidate_config_cannot_drop_pending_product_binding(tmp_path: Path) -> None:
    executor = FakeExecutor(
        ActionExecutionOutcome(kind="unknown", error_code="not_called"),
        ActionExecutionOutcome(kind="succeeded", output={"recovered": True}),
    )
    router, plans, audit = _router(tmp_path, executor)
    workspace = tmp_path / "workspace"
    tool = router.bindings(source="builtin", source_id="harnessix.product")[0]
    pending = router.plan(invocation(tool), context(workspace))
    digest = canonical_digest("previous-action-config")

    report = await _recover_product_actions(
        router,
        audit,
        candidate_config_sha256=canonical_digest("candidate-action-config"),
        recovery_config_sha256=digest,
    )

    assert report.pending_approval_routes == 1
    assert report.reconciled_routes == report.ready_routes == 0
    candidate = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: workspace,
    )
    with pytest.raises(KernelError) as missing:
        _verify_active_product_bindings(candidate, audit)

    assert missing.value.code == "product_action_recovery_binding_unavailable"
    assert router.status(pending.plan.execution.plan_id).state == "pending_approval"
    assert executor.calls == executor.reconciliations == 0
    plans.close()
    audit.close()
