"""可信Action操作路由：执行、期限、取消、对账与宿主中断收敛。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRouteSnapshot,
    ActionRouteState,
)
from harnessix.trusted_actions.planning import decode_action_arguments
from harnessix.trusted_actions.public_errors import (
    execute_exception_outcome,
    reconcile_exception_code,
)

if TYPE_CHECKING:
    from harnessix.trusted_actions.router import TrustedActionRouter


async def execute_action(router: TrustedActionRouter, plan_id: UUID) -> ActionExecutionOutcome:
    """Claim已批准计划并执行一次；取消后对账，发送后失败保留未知效果而不重试。"""
    current, definition, arguments = router._prepare_execution(plan_id)
    plan = current.plan
    claim = router._audit.claim_operation(
        plan_id,
        phase="execute",
        timeout_seconds=router._execute_timeout_seconds,
    )
    cancelled: asyncio.CancelledError | None = None
    try:
        async with asyncio.timeout(remaining_action_seconds(claim.operation.deadline)):
            outcome = await definition.executor.execute(plan, arguments)
        outcome = ActionExecutionOutcome.model_validate_json(outcome.model_dump_json())
        router._validate_outcome_identity(plan, outcome)
        if outcome.kind == "manual_intervention":
            raise KernelError("action_outcome_invalid", "首次执行不能直接进入人工处置终态")
    except TimeoutError as error:
        kind, code = execute_exception_outcome(error, plan.binding.effect_class)
        outcome = ActionExecutionOutcome(
            kind=kind,
            external_action_id=plan.external_action_id,
            error_code=code,
        )
    except asyncio.CancelledError as error:
        cancelled = error
        kind, code = execute_exception_outcome(error, plan.binding.effect_class)
        outcome = ActionExecutionOutcome(
            kind=kind,
            external_action_id=plan.external_action_id,
            error_code=code,
        )
    except UncertainEffectError as error:
        kind, code = execute_exception_outcome(error, plan.binding.effect_class)
        outcome = ActionExecutionOutcome(
            kind=kind,
            external_action_id=plan.external_action_id,
            error_code=code,
        )
    except Exception as error:
        kind, code = execute_exception_outcome(error, plan.binding.effect_class)
        outcome = ActionExecutionOutcome(
            kind=kind,
            external_action_id=plan.external_action_id,
            error_code=code,
        )
    target = cast(ActionRouteState, outcome.kind)
    router._audit.complete_operation(
        claim,
        target=target,
        executor_id=plan.binding.executor_id,
        output_sha256=(canonical_digest(outcome.output) if outcome.output is not None else None),
        artifact_sha256=outcome.artifact_sha256,
        external_action_id=outcome.external_action_id,
        error_code=outcome.error_code,
    )
    if cancelled is not None:
        raise cancelled
    return outcome


async def reconcile_action(router: TrustedActionRouter, plan_id: UUID) -> ActionExecutionOutcome:
    current = router._audit.load(plan_id)
    plan = current.plan
    definition = router._matching_definition(plan)
    if plan.binding.recovery_mode == "none":
        outcome = ActionExecutionOutcome(
            kind="manual_intervention", error_code="reconciliation_not_supported"
        )
        router._audit.transition(
            plan_id,
            expected={"unknown"},
            target="manual_intervention",
            error_code=outcome.error_code,
            reconciliation=outcome.kind,
        )
        return outcome
    try:
        arguments = decode_action_arguments(definition, plan.invocation.arguments)
    except (KernelError, ValidationError, ValueError, TypeError):
        raise KernelError("action_audit_store_corrupt", "持久Action参数不再可解析") from None
    try:
        claim = router._audit.claim_operation(
            plan_id,
            phase="reconcile",
            timeout_seconds=router._reconcile_timeout_seconds,
            max_attempts=router._max_reconciliation_attempts,
        )
    except KernelError as error:
        if error.code != "action_reconciliation_exhausted":
            raise
        outcome = ActionExecutionOutcome(
            kind="manual_intervention",
            external_action_id=plan.external_action_id,
            error_code="reconciliation_attempts_exhausted",
        )
        router._audit.transition(
            plan_id,
            expected={"unknown"},
            target="manual_intervention",
            executor_id=plan.binding.executor_id,
            external_action_id=plan.external_action_id,
            error_code=outcome.error_code,
            reconciliation=outcome.kind,
        )
        return outcome
    cancelled: asyncio.CancelledError | None = None
    try:
        async with asyncio.timeout(remaining_action_seconds(claim.operation.deadline)):
            outcome = await definition.executor.reconcile(plan, arguments)
        outcome = ActionExecutionOutcome.model_validate_json(outcome.model_dump_json())
        router._validate_outcome_identity(plan, outcome)
    except TimeoutError as error:
        outcome = ActionExecutionOutcome(
            kind="unknown",
            external_action_id=plan.external_action_id,
            error_code=reconcile_exception_code(error),
        )
    except asyncio.CancelledError as error:
        cancelled = error
        outcome = ActionExecutionOutcome(
            kind="unknown",
            external_action_id=plan.external_action_id,
            error_code=reconcile_exception_code(error),
        )
    except Exception as error:
        outcome = ActionExecutionOutcome(
            kind="unknown",
            external_action_id=plan.external_action_id,
            error_code=reconcile_exception_code(error),
        )
    target = cast(ActionRouteState, outcome.kind)
    router._audit.complete_operation(
        claim,
        target=target,
        executor_id=plan.binding.executor_id,
        output_sha256=(canonical_digest(outcome.output) if outcome.output is not None else None),
        artifact_sha256=outcome.artifact_sha256,
        external_action_id=outcome.external_action_id,
        error_code=outcome.error_code,
        reconciliation=outcome.kind,
    )
    if cancelled is not None:
        raise cancelled
    return outcome


def recover_interrupted_actions(router: TrustedActionRouter) -> tuple[UUID, ...]:
    recovered: list[UUID] = []
    for current in router._audit.active():
        was_interrupted = current.state in {"running", "reconciling"}
        recovered_route = recover_interrupted_action(router, current.plan.execution.plan_id)
        if was_interrupted and recovered_route.state == "unknown":
            recovered.append(current.plan.execution.plan_id)
    return tuple(recovered)


def recover_interrupted_action(router: TrustedActionRouter, plan_id: UUID) -> ActionRouteSnapshot:
    """把单个失去执行Owner的Route保守收敛为unknown，不触发Executor。"""

    current = router._audit.load(plan_id)
    if current.state not in {"running", "reconciling"}:
        return current
    return router._audit.interrupt_operation(plan_id, error_code="host_interrupted")


def remaining_action_seconds(deadline: datetime) -> float:
    remaining = (deadline - utc_now()).total_seconds()
    if remaining <= 0:
        raise TimeoutError
    return remaining
