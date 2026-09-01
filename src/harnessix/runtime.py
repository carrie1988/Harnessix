from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from harnessix.domain.errors import UncertainEffectError
from harnessix.domain.models import (
    ActionEvent,
    ActionFailure,
    ActionRequest,
    ActionResult,
    ActionSnapshot,
    ActionStatus,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    ExecutionOutcomeKind,
    PolicyDecisionKind,
    ReconciliationOutcomeKind,
    ToolDescriptor,
    utc_now,
)
from harnessix.domain.ports import EffectJournal, PolicyEngine
from harnessix.domain.registry import ToolDefinition, ToolRegistry

_SENSITIVE_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "secret_key",
        "token",
    }
)


def action_fingerprint(request: ActionRequest) -> str:
    """计算业务幂等指纹；忽略 action_id 和每次运行都可能变化的上下文。"""
    payload = {
        "spec_version": request.spec_version,
        "tenant_id": request.principal.tenant_id,
        "tool": request.tool,
        "arguments": request.arguments,
        "effect_hint": request.effect_hint,
        "secret_refs": [reference.model_dump(mode="json") for reference in request.secret_refs],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _find_sensitive_path(value: Any, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            current_path = f"{path}.{key}" if path else str(key)
            if normalized in _SENSITIVE_KEYS:
                return current_path
            found = _find_sensitive_path(child, current_path)
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            found = _find_sensitive_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


class ActionService:
    def __init__(
        self,
        *,
        journal: EffectJournal,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        lease_seconds: int = 30,
        worker_id: str | None = None,
        auto_execute: bool = True,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        self.journal = journal
        self.registry = registry
        self.policy_engine = policy_engine
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self.auto_execute = auto_execute

    async def initialize(self) -> list[UUID]:
        await self.journal.initialize()
        return await self.journal.recover_expired()

    async def close(self) -> None:
        await self.journal.close()

    async def submit(self, request: ActionRequest) -> ActionSnapshot:
        tool = self.registry.get(request.tool)
        snapshot, created = await self.journal.create_action(
            request, tool.descriptor(), action_fingerprint(request)
        )
        if not created:
            return snapshot

        validation_error = self._validate_request(request, tool)
        if validation_error is not None:
            return await self._fail_validation(snapshot, validation_error)

        snapshot = await self.journal.transition(
            request.action_id,
            expected={ActionStatus.RECEIVED},
            target=ActionStatus.VALIDATED,
            event_type="action_validated",
        )
        try:
            policy = await self.policy_engine.evaluate(snapshot, tool.descriptor())
        except Exception as error:
            return await self.journal.transition(
                request.action_id,
                expected={ActionStatus.VALIDATED},
                target=ActionStatus.FAILED,
                event_type="policy_evaluation_failed",
                result=ActionResult(
                    status=ActionStatus.FAILED,
                    error=ActionFailure(code="policy_error", message=str(error), retriable=True),
                ),
            )
        snapshot = await self.journal.transition(
            request.action_id,
            expected={ActionStatus.VALIDATED},
            target=ActionStatus.POLICY_EVALUATED,
            event_type="policy_evaluated",
            policy=policy,
            data={"policy_id": policy.policy_id, "decision": policy.kind.value},
        )
        if policy.kind is PolicyDecisionKind.DENY:
            return await self.journal.transition(
                request.action_id,
                expected={ActionStatus.POLICY_EVALUATED},
                target=ActionStatus.DENIED,
                event_type="action_denied",
                result=ActionResult(
                    status=ActionStatus.DENIED,
                    error=ActionFailure(
                        code="policy_denied", message=policy.reason, retriable=False
                    ),
                ),
            )
        if policy.kind is PolicyDecisionKind.REQUIRE_APPROVAL:
            return await self.journal.transition(
                request.action_id,
                expected={ActionStatus.POLICY_EVALUATED},
                target=ActionStatus.PENDING_APPROVAL,
                event_type="approval_requested",
            )
        snapshot = await self.journal.transition(
            request.action_id,
            expected={ActionStatus.POLICY_EVALUATED},
            target=ActionStatus.READY,
            event_type="action_ready",
        )
        if not self.auto_execute:
            return snapshot
        return await self._claim_and_execute(snapshot)

    async def decide_approval(
        self, action_id: UUID | str, decision: ApprovalDecision
    ) -> ActionSnapshot:
        snapshot = await self.journal.get_action(action_id)
        approval = ApprovalRecord(
            outcome=decision.outcome,
            actor=decision.actor,
            reason=decision.reason,
            request_fingerprint=snapshot.request_fingerprint,
        )
        if decision.outcome is ApprovalOutcome.REJECTED:
            return await self.journal.transition(
                action_id,
                expected={ActionStatus.PENDING_APPROVAL},
                target=ActionStatus.DENIED,
                event_type="approval_rejected",
                approval=approval,
                result=ActionResult(
                    status=ActionStatus.DENIED,
                    error=ActionFailure(
                        code="approval_rejected",
                        message=decision.reason or "审批人拒绝执行",
                        retriable=False,
                    ),
                ),
            )

        snapshot = await self.journal.transition(
            action_id,
            expected={ActionStatus.PENDING_APPROVAL},
            target=ActionStatus.READY,
            event_type="approval_granted",
            approval=approval,
        )
        if not self.auto_execute:
            return snapshot
        return await self._claim_and_execute(snapshot)

    async def reconcile(self, action_id: UUID | str) -> ActionSnapshot:
        snapshot = await self.journal.get_action(action_id)
        tool = self.registry.get(snapshot.request.tool)
        if not tool.supports_reconciliation:
            return await self.journal.transition(
                action_id,
                expected={ActionStatus.UNKNOWN},
                target=ActionStatus.MANUAL_INTERVENTION,
                event_type="reconciliation_unsupported",
                result=ActionResult(
                    status=ActionStatus.MANUAL_INTERVENTION,
                    error=ActionFailure(
                        code="reconciliation_not_supported",
                        message="该工具没有实现自动对账",
                        retriable=False,
                    ),
                ),
                clear_lease=True,
            )

        snapshot = await self.journal.transition(
            action_id,
            expected={ActionStatus.UNKNOWN},
            target=ActionStatus.RECONCILING,
            event_type="reconciliation_started",
            lease_owner=self.worker_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.lease_seconds),
        )
        try:
            outcome = await tool.executor.reconcile(snapshot)
        except Exception as error:  # 对账异常仍然不能证明原副作用失败
            return await self.journal.transition(
                action_id,
                expected={ActionStatus.RECONCILING},
                target=ActionStatus.UNKNOWN,
                event_type="reconciliation_uncertain",
                result=ActionResult(
                    status=ActionStatus.UNKNOWN,
                    error=ActionFailure(
                        code="reconciliation_error", message=str(error), retriable=False
                    ),
                ),
                clear_lease=True,
                required_lease_owner=self.worker_id,
            )

        if outcome.kind is ReconciliationOutcomeKind.SUCCEEDED:
            target = ActionStatus.SUCCEEDED
        elif outcome.kind is ReconciliationOutcomeKind.FAILED:
            target = ActionStatus.FAILED
        elif outcome.kind is ReconciliationOutcomeKind.MANUAL_INTERVENTION:
            target = ActionStatus.MANUAL_INTERVENTION
        else:
            target = ActionStatus.UNKNOWN
        return await self.journal.transition(
            action_id,
            expected={ActionStatus.RECONCILING},
            target=target,
            event_type="reconciliation_completed",
            result=ActionResult(
                status=target,
                output=outcome.output,
                error=outcome.error,
                receipt=outcome.receipt,
            ),
            data={"outcome": outcome.kind.value},
            clear_lease=True,
            required_lease_owner=self.worker_id,
        )

    async def get(self, action_id: UUID | str) -> ActionSnapshot:
        return await self.journal.get_action(action_id)

    async def events(self, action_id: UUID | str) -> list[ActionEvent]:
        return await self.journal.list_events(action_id)

    def tools(self) -> list[ToolDescriptor]:
        return self.registry.list_descriptors()

    def _validate_request(
        self, request: ActionRequest, tool: ToolDefinition
    ) -> ActionFailure | None:
        if request.effect_hint is not None and request.effect_hint is not tool.effect_class:
            return ActionFailure(
                code="effect_mismatch",
                message=(
                    f"调用方副作用提示为 {request.effect_hint.value}，"
                    f"运行时定义为 {tool.effect_class.value}"
                ),
                retriable=False,
            )
        if tool.requires_idempotency and request.idempotency_key is None:
            return ActionFailure(
                code="idempotency_key_required",
                message=f"工具 {tool.name} 必须提供 idempotency_key",
                retriable=False,
            )
        sensitive_path = _find_sensitive_path(
            {"arguments": request.arguments, "metadata": request.metadata}
        )
        if sensitive_path is not None:
            return ActionFailure(
                code="raw_secret_rejected",
                message=f"检测到疑似明文凭据字段：{sensitive_path}；请改用 secret_refs",
                retriable=False,
            )
        try:
            tool.input_model.model_validate(request.arguments)
        except ValidationError as error:
            return ActionFailure(code="invalid_arguments", message=str(error), retriable=False)
        return None

    async def _fail_validation(
        self, snapshot: ActionSnapshot, failure: ActionFailure
    ) -> ActionSnapshot:
        return await self.journal.transition(
            snapshot.request.action_id,
            expected={ActionStatus.RECEIVED},
            target=ActionStatus.FAILED,
            event_type="validation_failed",
            data={"code": failure.code},
            result=ActionResult(status=ActionStatus.FAILED, error=failure),
        )

    async def _claim_and_execute(self, snapshot: ActionSnapshot) -> ActionSnapshot:
        action_id = snapshot.request.action_id
        snapshot = await self.journal.transition(
            action_id,
            expected={ActionStatus.READY},
            target=ActionStatus.LEASED,
            event_type="execution_leased",
            lease_owner=self.worker_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.lease_seconds),
        )
        return await self.execute_leased(snapshot)

    async def execute_leased(self, snapshot: ActionSnapshot) -> ActionSnapshot:
        """执行已经由当前 Worker Claim 的 Action。"""
        action_id = snapshot.request.action_id
        tool = self.registry.get(snapshot.request.tool)
        snapshot = await self.journal.transition(
            action_id,
            expected={ActionStatus.LEASED},
            target=ActionStatus.RUNNING,
            event_type="execution_started",
            required_lease_owner=self.worker_id,
        )
        try:
            arguments: BaseModel = tool.input_model.model_validate(snapshot.request.arguments)
            outcome = await tool.executor.execute(snapshot, arguments)
        except UncertainEffectError as error:
            outcome_kind = ExecutionOutcomeKind.UNKNOWN
            result = ActionResult(
                status=ActionStatus.UNKNOWN,
                error=ActionFailure(
                    code="uncertain_external_effect", message=str(error), retriable=False
                ),
            )
        except Exception as error:
            if tool.effect_class is EffectClass.READ_ONLY:
                outcome_kind = ExecutionOutcomeKind.FAILED
                result = ActionResult(
                    status=ActionStatus.FAILED,
                    error=ActionFailure(code="executor_error", message=str(error), retriable=True),
                )
            else:
                outcome_kind = ExecutionOutcomeKind.UNKNOWN
                result = ActionResult(
                    status=ActionStatus.UNKNOWN,
                    error=ActionFailure(
                        code="unexpected_write_error", message=str(error), retriable=False
                    ),
                )
        else:
            outcome_kind = outcome.kind
            target_status = {
                ExecutionOutcomeKind.SUCCEEDED: ActionStatus.SUCCEEDED,
                ExecutionOutcomeKind.FAILED: ActionStatus.FAILED,
                ExecutionOutcomeKind.UNKNOWN: ActionStatus.UNKNOWN,
            }[outcome.kind]
            result = ActionResult(
                status=target_status,
                output=outcome.output,
                error=outcome.error,
                receipt=outcome.receipt,
            )

        target = {
            ExecutionOutcomeKind.SUCCEEDED: ActionStatus.SUCCEEDED,
            ExecutionOutcomeKind.FAILED: ActionStatus.FAILED,
            ExecutionOutcomeKind.UNKNOWN: ActionStatus.UNKNOWN,
        }[outcome_kind]
        return await self.journal.transition(
            action_id,
            expected={ActionStatus.RUNNING},
            target=target,
            event_type="execution_completed",
            data={"outcome": outcome_kind.value},
            result=result,
            clear_lease=True,
            required_lease_owner=self.worker_id,
        )
