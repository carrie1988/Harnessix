"""Trusted Action公开错误泄漏回归：任何内部异常/敏感式样不得进入公开边界。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import BaseModel

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.public_errors import sanitize_plan_exception
from harnessix.trusted_actions.router import TrustedActionDefinition, TrustedActionRouter
from tests.trusted_actions.test_router import (
    FakeExecutor,
    FileInput,
    binding,
    context,
    definition,
    invocation,
    router,
)

SECRET_PATTERN = "hxak-leak-9f8e7d6c"
PATH_PATTERN = "/Users/leak/internal/secret.txt"
WIN_PATH_PATTERN = "C:\\leak\\internal\\secret.txt"
ARGV_PATTERN = "--private-flag=abc123xyz"
EXCEPTION_PATTERN = "psycopg.OperationalError: connection boom"
PATTERNS = (SECRET_PATTERN, PATH_PATTERN, WIN_PATH_PATTERN, ARGV_PATTERN, EXCEPTION_PATTERN)


def _payload() -> str:
    return " | ".join(PATTERNS)


def _assert_no_leak(*blobs: str | bytes) -> None:
    for blob in blobs:
        data = blob if isinstance(blob, bytes) else blob.encode()
        for pattern in PATTERNS:
            assert pattern.encode() not in data


@dataclass
class LeakingExecutor:
    stage: str = "execute"
    calls: int = 0
    reconciliations: int = 0

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.calls += 1
        FileInput.model_validate(arguments)
        if self.stage == "uncertain":
            raise UncertainEffectError(_payload())
        if self.stage == "reconcile":
            return ActionExecutionOutcome(kind="unknown", error_code="write_effect_response_lost")
        raise RuntimeError(_payload())

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.reconciliations += 1
        FileInput.model_validate(arguments)
        raise RuntimeError(_payload())


def _write_binding():
    return binding(
        source_id="harnessix.product",
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        risk=RiskLevel.HIGH,
        recovery="durable_ledger",
    )


def _store_bytes(*paths: Path) -> list[bytes]:
    return [path.read_bytes() for path in paths if path.exists()]


def test_plan_resolve_exception_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = _write_binding()

    def resolve(arguments: BaseModel, _: object) -> NoReturn:
        raise RuntimeError(_payload())

    actions, plans, audit = router(root)
    actions.register(TrustedActionDefinition(tool, FileInput, resolve, FakeExecutor(
        ActionExecutionOutcome(kind="succeeded")
    )))
    with pytest.raises(KernelError) as caught:
        actions.plan(invocation(tool), context(root))
    assert caught.value.code == "action_plan_failed"
    _assert_no_leak(str(caught.value), repr(caught.value), *_store_bytes(
        root.parent / "state" / "plans.db", root.parent / "state" / "audit.db"
    ))


def test_plan_policy_exception_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = _write_binding()
    plans_path = root.parent / "state" / "plans.db"
    audit_path = root.parent / "state" / "audit.db"
    from harnessix.execution.store import SQLiteExecutionPlanStore
    from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
    from harnessix.trusted_actions.store import SQLiteActionAuditStore

    class BrokenPolicy(DefaultCodingRiskPolicy):
        def evaluate(self, *args, **kwargs) -> NoReturn:
            raise RuntimeError(_payload())

    actions = TrustedActionRouter(
        plans=SQLiteExecutionPlanStore(plans_path),
        audit=SQLiteActionAuditStore(audit_path),
        workspace_root=lambda _: root,
        policy=BrokenPolicy(),
    )
    actions.register(definition(tool, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))))
    with pytest.raises(KernelError) as caught:
        actions.plan(invocation(tool), context(root))
    assert caught.value.code == "action_plan_failed"
    _assert_no_leak(str(caught.value), repr(caught.value), *_store_bytes(plans_path, audit_path))


async def test_execute_exception_leaves_only_public_codes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = _write_binding()
    actions, plans, audit = router(root)
    executor = LeakingExecutor()
    actions.register(definition(tool, executor))
    route = actions.plan(invocation(tool), context(root))
    plan_id = route.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.APPROVED, actor="leak-test", reason="leak-regression"
        ),
    )
    outcome = await actions.execute(plan_id)
    assert outcome.kind == "unknown" and outcome.error_code == "unexpected_write_error"
    snapshot = actions.status(plan_id)
    events = actions.events(plan_id)
    _assert_no_leak(
        repr(outcome),
        snapshot.model_dump_json(),
        "[" + ",".join(event.model_dump_json() for event in events) + "]",
        *_store_bytes(root.parent / "state" / "plans.db", root.parent / "state" / "audit.db"),
    )


async def test_uncertain_effect_error_leaves_only_public_codes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = _write_binding()
    actions, plans, audit = router(root)
    executor = LeakingExecutor(stage="uncertain")
    actions.register(definition(tool, executor))
    route = actions.plan(invocation(tool), context(root))
    plan_id = route.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.APPROVED, actor="leak-test", reason="leak-regression"
        ),
    )
    outcome = await actions.execute(plan_id)
    assert outcome.kind == "unknown" and outcome.error_code == "uncertain_external_effect"
    _assert_no_leak(
        repr(outcome),
        actions.status(plan_id).model_dump_json(),
        *_store_bytes(root.parent / "state" / "plans.db", root.parent / "state" / "audit.db"),
    )


async def test_reconcile_exception_leaves_only_public_codes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    tool = _write_binding()
    actions, plans, audit = router(root)
    executor = LeakingExecutor(stage="reconcile")
    actions.register(definition(tool, executor))
    route = actions.plan(invocation(tool), context(root))
    plan_id = route.plan.execution.plan_id
    actions.decide(
        plan_id,
        ApprovalDecision(
            outcome=ApprovalOutcome.APPROVED, actor="leak-test", reason="leak-regression"
        ),
    )
    outcome = await actions.execute(plan_id)
    assert outcome.kind == "unknown"
    reconciled = await actions.reconcile(plan_id)
    assert reconciled.kind == "unknown" and reconciled.error_code == "reconciliation_error"
    snapshot = actions.status(plan_id)
    events = actions.events(plan_id)
    _assert_no_leak(
        repr(reconciled),
        snapshot.model_dump_json(),
        "[" + ",".join(event.model_dump_json() for event in events) + "]",
        *_store_bytes(root.parent / "state" / "plans.db", root.parent / "state" / "audit.db"),
    )


def test_kernel_error_passes_through_and_other_exceptions_are_closed() -> None:
    original = KernelError("stable_code", "稳定公开消息")
    assert sanitize_plan_exception(original) is original
    sanitized = sanitize_plan_exception(RuntimeError(_payload()))
    assert sanitized.code == "action_plan_failed"
    _assert_no_leak(str(sanitized), repr(sanitized))
