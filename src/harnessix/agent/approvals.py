from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.models import (
    ApprovalContent,
    Item,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    Thread,
    ToolCallContent,
    Turn,
)
from harnessix.domain.models import EffectClass, ToolDescriptor, utc_now
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from harnessix.patches.contracts import PatchProposal
from harnessix.processes.bridge_contracts import AgentProcessCallPlan
from harnessix.processes.contracts import ProcessRequest
from harnessix.tools.workspace import digest

READ_ONLY_POLICY_VERSION = "kernel-read-only/v1"


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def tool_fingerprint(definition: ToolDescriptor) -> str:
    return _fingerprint(definition.model_dump(mode="json"))


def request_fingerprint(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    *,
    policy_version: str = READ_ONLY_POLICY_VERSION,
) -> str:
    """绑定当前只读契约；不是 Workspace 内容哈希或未来的 OS 授权凭证。"""
    return execution_fingerprint(
        thread.thread_id, turn.turn_id, thread.workspace, call, policy_version=policy_version
    )


def execution_fingerprint(
    thread_id: UUID,
    turn_id: UUID,
    workspace: str,
    call: ToolCallContent,
    *,
    policy_version: str = READ_ONLY_POLICY_VERSION,
) -> str:
    """审批和进程内作用域共享同一摘要格式，不引入第二套权限。"""
    return _fingerprint(
        {
            "policy": policy_version,
            "thread_id": str(thread_id),
            "turn_id": str(turn_id),
            "workspace": workspace,
            "call": call.model_dump(mode="json"),
        }
    )


def approval_for(turn: Turn, call: ToolCallContent) -> Item | None:
    return next(
        (
            item
            for item in turn.items
            if isinstance(item.content, ApprovalContent) and item.content.call_id == call.call_id
        ),
        None,
    )


def validate_patch_plan(
    thread: Thread, turn: Turn, call: ToolCallContent, plan: ManagedPatchCallPlan
) -> bool:
    """只校验事件归属和提案一致性；磁盘事实由受管桥接核对。"""
    try:
        checked = ManagedPatchCallPlan.model_validate_json(plan.model_dump_json())
        proposal = PatchProposal.model_validate_json(json.dumps(call.arguments, allow_nan=False))
    except (ValidationError, ValueError, TypeError):
        return False
    return (
        call.tool == "apply_patch"
        and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
        and call.requires_approval
        and call.tool_fingerprint is not None
        and (checked.thread_id, checked.turn_id, checked.call_id)
        == (thread.thread_id, turn.turn_id, call.call_id)
        and checked.call_fingerprint == request_fingerprint(thread, turn, call)
        and checked.manifest.proposal_sha256 == _fingerprint(proposal.model_dump(mode="json"))
    )


def validate_batch_plan(
    thread: Thread, turn: Turn, call: ToolCallContent, plan: ManagedPatchBatchCallPlan
) -> bool:
    try:
        checked = ManagedPatchBatchCallPlan.model_validate_json(plan.model_dump_json())
        proposal = PatchBatchProposal.model_validate_json(
            json.dumps(call.arguments, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        return False
    return (
        call.tool == "apply_patch_batch"
        and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
        and call.requires_approval
        and call.tool_fingerprint is not None
        and (checked.thread_id, checked.turn_id, checked.call_id)
        == (thread.thread_id, turn.turn_id, call.call_id)
        and checked.call_fingerprint == request_fingerprint(thread, turn, call)
        and checked.backend.manifest.proposal_sha256
        == _fingerprint(proposal.model_dump(mode="json"))
    )


def validate_process_plan(
    thread: Thread, turn: Turn, call: ToolCallContent, plan: AgentProcessCallPlan
) -> bool:
    """校验Session调用归属和公开命令摘要；Action事实由宿主桥接另行核对。"""
    try:
        checked = AgentProcessCallPlan.model_validate_json(plan.model_dump_json())
        process = ProcessRequest.model_validate_json(json.dumps(call.arguments, allow_nan=False))
    except (ValidationError, ValueError, TypeError):
        return False
    return (
        call.tool == "host.process"
        and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
        and call.requires_approval
        and call.tool_fingerprint is not None
        and (checked.thread_id, checked.turn_id, checked.call_id)
        == (thread.thread_id, turn.turn_id, call.call_id)
        and checked.workspace == thread.workspace
        and checked.call_fingerprint == request_fingerprint(thread, turn, call)
        and checked.action_tool_version == call.tool_version
        and checked.program == process.program
        and checked.arguments_sha256 == digest(process.arguments)
        and checked.timeout_seconds == process.timeout_seconds
    )


def approval_matches(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    content: ApprovalContent,
) -> bool:
    if isinstance(content, ProcessApprovalRequestContent):
        return (
            validate_process_plan(thread, turn, call, content.plan)
            and content.request_fingerprint == content.plan.approval_fingerprint
        )
    if isinstance(content, PatchBatchApprovalRequestContent):
        return (
            validate_batch_plan(thread, turn, call, content.plan)
            and content.request_fingerprint == content.plan.approval_fingerprint
        )
    if isinstance(content, PatchApprovalRequestContent):
        return (
            validate_patch_plan(thread, turn, call, content.plan)
            and content.request_fingerprint == content.plan.approval_fingerprint
        )
    return content.request_fingerprint == request_fingerprint(
        thread, turn, call, policy_version=content.policy_version
    )


def remaining_seconds(turn: Turn) -> float:
    # 持久墙钟截止时间：暂停、离线和重启均不刷新原 Turn 的预算。
    return (
        turn.created_at + timedelta(seconds=turn.budget.timeout_seconds) - utc_now()
    ).total_seconds()
