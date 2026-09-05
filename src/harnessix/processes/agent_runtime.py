"""Agent与Action Plane的受信运行桥；不执行、轮询或发布完整进程输出。"""

from __future__ import annotations

from typing import Any, Literal, cast
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    PROCESS_RESOLVED_STATUSES,
    PROCESS_WAITING_STATUSES,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.agent.ports import ProcessObservation
from harnessix.domain.errors import HarnessixError
from harnessix.domain.models import (
    ActionFailure,
    ActionSnapshot,
    ActionStatus,
    ApprovalDecision,
    EffectClass,
    Principal,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.processes.action_executor import ProcessActionInput
from harnessix.processes.agent_bridge import prepare_process_action, process_snapshot_matches
from harnessix.processes.bridge_contracts import (
    AgentProcessCallPlan,
    process_binding_from_version,
)
from harnessix.processes.contracts import ProcessResult
from harnessix.processes.session_projection import (
    process_action_state,
    process_approval_decision,
    process_approval_request,
)
from harnessix.runtime import ActionService


class ProcessAgentBridge:
    """把Agent调用映射到唯一Action；Action Worker必须由宿主独立运行。"""

    def __init__(self, service: ActionService, principal: Principal) -> None:
        if service.auto_execute:
            raise KernelError(
                "process_auto_execute_forbidden", "Agent进程桥必须使用独立Worker，禁止审批内执行"
            )
        definitions = [
            definition for definition in service.tools() if definition.name == "host.process"
        ]
        if len(definitions) != 1:
            raise KernelError("process_tool_not_found", "Action Plane必须唯一注册host.process")
        definition = definitions[0]
        try:
            process_binding_from_version(definition.version)
        except ValueError:
            raise KernelError(
                "process_contract_invalid", "Action Plane的host.process缺少有效宿主绑定"
            ) from None
        if (
            definition.input_schema != ProcessActionInput.model_json_schema()
            or definition.effect_class is not EffectClass.NON_IDEMPOTENT_WRITE
            or definition.risk_level is not RiskLevel.HIGH
            or not definition.requires_idempotency
            or not definition.requires_approval
            or definition.supports_reconciliation
        ):
            raise KernelError(
                "process_contract_invalid", "Action Plane的host.process不符合强制审批契约"
            )
        self._service = service
        self._definition = definition.model_copy(deep=True)
        self._principal = Principal.model_validate_json(principal.model_dump_json())

    def definition(self) -> ToolDescriptor:
        return self._definition.model_copy(deep=True)

    async def prepare(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
        *,
        approval_id: UUID,
    ) -> ProcessApprovalRequestContent | ToolResultContent:
        cancel.checkpoint()
        prepared = prepare_process_action(call, scope, self._definition, self._principal)
        snapshot = await self._service.submit(prepared.request)
        cancel.checkpoint()
        self._require_snapshot(call, scope, prepared.plan, snapshot)
        if snapshot.status is ActionStatus.PENDING_APPROVAL and snapshot.approval is None:
            return process_approval_request(
                call,
                scope,
                self._definition,
                self._principal,
                prepared.plan,
                snapshot,
                approval_id=approval_id,
            )
        if (
            snapshot.status in {ActionStatus.DENIED, ActionStatus.FAILED}
            and snapshot.approval is None
        ):
            return self._admission_failure(call, snapshot)
        raise KernelError(
            "process_admission_invalid", "Process Action未进入唯一待审批状态；禁止继续"
        )

    async def decide(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        approval: ProcessApprovalRequestContent,
        decision: ApprovalDecision,
        cancel: CancelToken,
    ) -> ProcessApprovalRequestContent:
        decision = ApprovalDecision.model_validate_json(decision.model_dump_json())
        cancel.checkpoint()
        snapshot = await self._service.get(approval.plan.action_id)
        self._require_snapshot(call, scope, approval.plan, snapshot)
        if snapshot.approval is None:
            if snapshot.status is not ActionStatus.PENDING_APPROVAL:
                raise KernelError("process_projection_closed", "Action审批已关闭但缺少决定事实")
            try:
                snapshot = await self._service.decide_approval(approval.plan.action_id, decision)
            except HarnessixError as error:
                if error.code not in {"action_conflict", "illegal_transition"}:
                    raise
                snapshot = await self._service.get(approval.plan.action_id)
        cancel.checkpoint()
        self._require_snapshot(call, scope, approval.plan, snapshot)
        self._require_decision(snapshot, decision)
        return process_approval_decision(
            approval, call, scope, self._definition, self._principal, snapshot
        )

    async def sync_decision(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        approval: ProcessApprovalRequestContent,
        cancel: CancelToken,
    ) -> ProcessApprovalRequestContent | None:
        cancel.checkpoint()
        snapshot = await self._service.get(approval.plan.action_id)
        cancel.checkpoint()
        self._require_snapshot(call, scope, approval.plan, snapshot)
        if snapshot.approval is None:
            if snapshot.status is ActionStatus.PENDING_APPROVAL:
                return None
            raise KernelError("process_projection_closed", "Action审批已关闭但缺少决定事实")
        return process_approval_decision(
            approval, call, scope, self._definition, self._principal, snapshot
        )

    async def observe(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        approval: ProcessApprovalRequestContent,
        cancel: CancelToken,
    ) -> ProcessObservation:
        cancel.checkpoint()
        snapshot = await self._service.get(approval.plan.action_id)
        cancel.checkpoint()
        self._require_snapshot(call, scope, approval.plan, snapshot)
        if snapshot.status in PROCESS_WAITING_STATUSES:
            if snapshot.result is not None:
                raise KernelError("process_projection_mismatch", "运行中Action不能携带终态结果")
        elif snapshot.status in PROCESS_RESOLVED_STATUSES:
            if snapshot.result is None or snapshot.result.status is not snapshot.status:
                raise KernelError("process_projection_incomplete", "Action终态与结果事实不一致")
            if snapshot.status is ActionStatus.SUCCEEDED and snapshot.result.error is not None:
                raise KernelError("process_projection_mismatch", "成功Action不能携带失败事实")
        else:
            raise KernelError("process_projection_pending", "Action尚未形成可观察的审批后状态")
        state = process_action_state(
            approval,
            call,
            scope,
            self._definition,
            self._principal,
            snapshot,
            origin="recovery",
        )
        process = self._process_result(snapshot)
        result = (
            self._terminal_result(call, snapshot, state, process)
            if snapshot.result is not None
            else None
        )
        return ProcessObservation(state=state, result=result, process=process)

    def _require_snapshot(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: AgentProcessCallPlan,
        snapshot: ActionSnapshot,
    ) -> None:
        if not process_snapshot_matches(
            call, scope, self._definition, self._principal, plan, snapshot
        ):
            raise KernelError("process_projection_mismatch", "Action事实与Agent进程计划不匹配")

    @staticmethod
    def _require_decision(snapshot: ActionSnapshot, decision: ApprovalDecision) -> None:
        recorded = snapshot.approval
        if recorded is None:
            raise KernelError("process_projection_pending", "Action审批决定尚未持久化")
        if (recorded.outcome, recorded.actor, recorded.reason) != (
            decision.outcome,
            decision.actor,
            decision.reason,
        ):
            raise KernelError("approval_conflict", "Action审批已绑定其他决定")

    @staticmethod
    def _failure(error: ActionFailure | None, fallback: str) -> AgentFailure:
        if error is None:
            return AgentFailure(code=fallback, message="Process Action未成功")
        return AgentFailure(code=error.code, message=error.message, retryable=error.retriable)

    @classmethod
    def _admission_failure(
        cls, call: ToolCallContent, snapshot: ActionSnapshot
    ) -> ToolResultContent:
        error = snapshot.result.error if snapshot.result is not None else None
        return ToolResultContent(
            call_id=call.call_id,
            outcome="failed",
            error=cls._failure(error, "process_admission_failed"),
            action_id=snapshot.request.action_id,
        )

    @staticmethod
    def _process_result(snapshot: ActionSnapshot) -> ProcessResult | None:
        if snapshot.result is None or snapshot.result.output is None:
            return None
        try:
            return ProcessResult.model_validate(snapshot.result.output)
        except ValidationError:
            if snapshot.status in {ActionStatus.SUCCEEDED, ActionStatus.UNKNOWN}:
                raise KernelError(
                    "process_result_invalid", "Action结果不是有效的ProcessResult"
                ) from None
            return None

    @classmethod
    def _terminal_result(
        cls,
        call: ToolCallContent,
        snapshot: ActionSnapshot,
        state: ProcessActionStateContent,
        process: ProcessResult | None,
    ) -> ToolResultContent:
        assert snapshot.result is not None
        status = snapshot.status
        outcome = cast(
            Literal["succeeded", "failed", "unknown"],
            {
                ActionStatus.DENIED: "failed",
                ActionStatus.SUCCEEDED: "succeeded",
                ActionStatus.FAILED: "failed",
                ActionStatus.UNKNOWN: "unknown",
                ActionStatus.MANUAL_INTERVENTION: "unknown",
            }[status],
        )
        output: Any = None
        if process is not None:
            output = {
                "action_status": status.value,
                "returncode": process.returncode,
                "stop_reason": process.stop_reason,
                "termination": process.termination,
                "stdout": {
                    "captured_bytes": process.stdout.captured_bytes,
                    "observed_bytes": process.stdout.observed_bytes,
                    "observed_sha256": process.stdout.observed_sha256,
                    "truncated": process.stdout.truncated,
                    "eof": process.stdout.eof,
                },
                "stderr": {
                    "captured_bytes": process.stderr.captured_bytes,
                    "observed_bytes": process.stderr.observed_bytes,
                    "observed_sha256": process.stderr.observed_sha256,
                    "truncated": process.stderr.truncated,
                    "eof": process.stderr.eof,
                },
            }
        return ToolResultContent(
            call_id=call.call_id,
            outcome=outcome,
            output=output,
            error=(
                None
                if outcome == "succeeded"
                else cls._failure(snapshot.result.error, f"process_{status.value}")
            ),
            action_id=snapshot.request.action_id,
            process=state.effect,
        )
