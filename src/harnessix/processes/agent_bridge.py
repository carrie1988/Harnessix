"""Agent与Process Action的受信准备/核对桥接；不写审批或执行进程。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import (
    ActionContext,
    ActionRequest,
    ActionSnapshot,
    ActionStatus,
    ApprovalOutcome,
    EffectClass,
    Principal,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.processes.action_executor import ProcessActionInput
from harnessix.processes.bridge_contracts import (
    PROCESS_ACTION_NAMESPACE,
    PROCESS_AGENT_POLICY,
    AgentProcessCallPlan,
    process_action_identity,
    process_binding_from_version,
    process_call_request_id,
)
from harnessix.tools.workspace import digest

_METADATA_KEY = "harnessix.agent_process"
_APPROVED_ACTION_STATUSES = frozenset(
    {
        ActionStatus.READY,
        ActionStatus.LEASED,
        ActionStatus.RUNNING,
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.UNKNOWN,
        ActionStatus.RECONCILING,
        ActionStatus.MANUAL_INTERVENTION,
    }
)
_UNDECIDED_ACTION_STATUSES = frozenset(
    {
        ActionStatus.RECEIVED,
        ActionStatus.VALIDATED,
        ActionStatus.POLICY_EVALUATED,
        ActionStatus.PENDING_APPROVAL,
        ActionStatus.DENIED,
        ActionStatus.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class PreparedProcessAction:
    """桥接准备结果；Action请求提交后才形成Effect Journal事实。"""

    request: ActionRequest
    plan: AgentProcessCallPlan


def _process_binding(version: str) -> str:
    try:
        return process_binding_from_version(version)
    except ValueError:
        raise KernelError("tool_contract_changed", "进程Action工具版本缺少有效宿主绑定") from None


def _validate_tool_call(
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
) -> ProcessActionInput:
    scope.validate_call(call)
    _process_binding(definition.version)
    if (
        definition.name != "host.process"
        or definition.input_schema != ProcessActionInput.model_json_schema()
        or definition.effect_class is not EffectClass.NON_IDEMPOTENT_WRITE
        or definition.risk_level is not RiskLevel.HIGH
        or not definition.requires_idempotency
        or not definition.requires_approval
        or definition.supports_reconciliation
        or call.tool != definition.name
        or call.tool_version != definition.version
        or call.effect_class is not definition.effect_class
        or call.requires_approval is not True
        or call.tool_fingerprint != tool_fingerprint(definition)
    ):
        raise KernelError("tool_contract_changed", "Agent进程调用与持久Action工具契约不一致")
    try:
        return ProcessActionInput.model_validate_json(json.dumps(call.arguments, allow_nan=False))
    except (ValidationError, ValueError, TypeError):
        raise KernelError("tool_invalid_arguments", "Process参数不符合严格JSON契约") from None


def prepare_process_action(
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
    principal: Principal,
) -> PreparedProcessAction:
    """确定性构造唯一Action；本函数不写Journal，也不产生执行许可。"""
    process = _validate_tool_call(call, scope, definition)
    request_id = process_call_request_id(
        scope.thread_id,
        scope.turn_id,
        scope.call_id,
        scope.workspace,
        scope.request_fingerprint,
    )
    principal_fingerprint = digest(principal.model_dump(mode="json"))
    base = ActionRequest(
        action_id=UUID(int=0),
        tool=definition.name,
        arguments=process.model_dump(mode="json"),
        principal=principal,
        context=ActionContext(session_id=str(scope.thread_id), run_id=str(scope.turn_id)),
        effect_hint=EffectClass.NON_IDEMPOTENT_WRITE,
        idempotency_key="pending",
    )
    # ActionService的公开指纹规则是Action幂等事实的一部分；避免在桥接层复制算法。
    from harnessix.runtime import action_fingerprint

    action_request_fingerprint = action_fingerprint(base)
    binding_fingerprint = _process_binding(definition.version)
    identity = process_action_identity(
        request_id,
        action_request_fingerprint,
        definition.version,
        binding_fingerprint,
        principal_fingerprint,
    )
    action_id = uuid5(PROCESS_ACTION_NAMESPACE, identity)
    idempotency_key = f"agent-process:{identity}"
    metadata = {
        _METADATA_KEY: {
            "version": PROCESS_AGENT_POLICY,
            "thread_id": str(scope.thread_id),
            "turn_id": str(scope.turn_id),
            "call_id": str(scope.call_id),
            "workspace": scope.workspace,
            "call_fingerprint": scope.request_fingerprint,
            "request_id": request_id,
        }
    }
    request = base.model_copy(
        update={
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "metadata": metadata,
        }
    )
    if action_fingerprint(request) != action_request_fingerprint:
        raise KernelError("tool_contract_changed", "Action指纹规则不再兼容进程桥接契约")
    data = {
        "version": PROCESS_AGENT_POLICY,
        "thread_id": str(scope.thread_id),
        "turn_id": str(scope.turn_id),
        "call_id": str(scope.call_id),
        "workspace": scope.workspace,
        "call_fingerprint": scope.request_fingerprint,
        "request_id": request_id,
        "action_id": str(action_id),
        "action_fingerprint": action_request_fingerprint,
        "action_tool_version": definition.version,
        "binding_fingerprint": binding_fingerprint,
        "principal_fingerprint": principal_fingerprint,
        "idempotency_key": idempotency_key,
        "program": process.program,
        "arguments_sha256": digest(process.arguments),
        "timeout_seconds": process.timeout_seconds,
    }
    plan = AgentProcessCallPlan.model_validate_json(
        json.dumps({**data, "approval_fingerprint": digest(data)}, allow_nan=False)
    )
    return PreparedProcessAction(request=request, plan=plan)


def process_snapshot_matches(
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
    principal: Principal,
    plan: AgentProcessCallPlan,
    snapshot: ActionSnapshot,
) -> bool:
    """核对跨库投影；只接受Effect Journal中原Action，不根据Session猜测状态。"""
    try:
        checked_plan = AgentProcessCallPlan.model_validate_json(plan.model_dump_json())
        prepared = prepare_process_action(call, scope, definition, principal)
    except (KernelError, ValidationError, ValueError, TypeError):
        return False
    approval = snapshot.approval
    if approval is None:
        approval_matches = snapshot.status in _UNDECIDED_ACTION_STATUSES
    elif approval.outcome is ApprovalOutcome.REJECTED:
        approval_matches = snapshot.status is ActionStatus.DENIED
    else:
        approval_matches = snapshot.status in _APPROVED_ACTION_STATUSES
    return (
        checked_plan == prepared.plan
        and snapshot.request == prepared.request
        and snapshot.request_fingerprint == prepared.plan.action_fingerprint
        and snapshot.tool == definition
        and approval_matches
        and (approval is None or approval.request_fingerprint == prepared.plan.action_fingerprint)
    )
