"""只从已核对ActionSnapshot构造Agent Session进程投影。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import (
    ProcessActionEffect,
    ProcessActionStateContent,
    ProcessApprovalRequestContent,
    ToolCallContent,
)
from harnessix.domain.models import ActionSnapshot, ActionStatus, Principal, ToolDescriptor
from harnessix.processes.agent_bridge import process_snapshot_matches
from harnessix.processes.bridge_contracts import AgentProcessCallPlan
from harnessix.tools.workspace import digest


def _require_snapshot(
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
    principal: Principal,
    plan: AgentProcessCallPlan,
    snapshot: ActionSnapshot,
) -> None:
    if not process_snapshot_matches(call, scope, definition, principal, plan, snapshot):
        raise KernelError("process_projection_mismatch", "Action事实与Agent进程计划不匹配")


def process_approval_request(
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
    principal: Principal,
    plan: AgentProcessCallPlan,
    snapshot: ActionSnapshot,
    *,
    approval_id: UUID,
) -> ProcessApprovalRequestContent:
    """只允许原Action的PENDING_APPROVAL形成Session等待请求。"""
    _require_snapshot(call, scope, definition, principal, plan, snapshot)
    if snapshot.status is not ActionStatus.PENDING_APPROVAL or snapshot.approval is not None:
        raise KernelError("process_projection_closed", "Action不处于待审批状态")
    return ProcessApprovalRequestContent(
        approval_id=approval_id,
        call_id=call.call_id,
        plan=plan,
        request_fingerprint=plan.approval_fingerprint,
    )


def process_approval_decision(
    original: ProcessApprovalRequestContent,
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
    principal: Principal,
    snapshot: ActionSnapshot,
) -> ProcessApprovalRequestContent:
    """镜像Action Journal决定；不接受客户端构造的ApprovalRecord。"""
    _require_snapshot(call, scope, definition, principal, original.plan, snapshot)
    if original.decision is not None:
        raise KernelError("process_projection_closed", "Session进程审批已完成")
    if snapshot.approval is None:
        raise KernelError("process_projection_pending", "Action审批决定尚未持久化")
    return ProcessApprovalRequestContent.model_validate_json(
        original.model_copy(
            update={"action_status": snapshot.status, "decision": snapshot.approval}
        ).model_dump_json()
    )


def process_action_state(
    approval: ProcessApprovalRequestContent,
    call: ToolCallContent,
    scope: ToolExecutionScope,
    definition: ToolDescriptor,
    principal: Principal,
    snapshot: ActionSnapshot,
    *,
    origin: Literal["execution", "recovery"],
) -> ProcessActionStateContent:
    """投影Action状态和结果摘要；完整结果仍只属于Effect Journal。"""
    _require_snapshot(call, scope, definition, principal, approval.plan, snapshot)
    if approval.decision is None or snapshot.approval != approval.decision:
        raise KernelError("process_projection_mismatch", "Session决定与Action审批事实不一致")
    result_fingerprint = (
        digest(snapshot.result.model_dump(mode="json")) if snapshot.result is not None else None
    )
    try:
        effect = ProcessActionEffect(
            plan_fingerprint=approval.plan.approval_fingerprint,
            action_id=approval.plan.action_id,
            action_fingerprint=approval.plan.action_fingerprint,
            status=snapshot.status,
            result_fingerprint=result_fingerprint,
            origin=origin,
        )
    except ValueError:
        raise KernelError("process_projection_incomplete", "Action状态缺少可验证结果事实") from None
    return ProcessActionStateContent(call_id=call.call_id, effect=effect)
