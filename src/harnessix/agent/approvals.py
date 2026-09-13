"""持久Agent状态机：校验审批请求指纹、有效期与决策绑定。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid5

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ApprovalContent,
    ApprovalRequestContent,
    Item,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    Thread,
    ToolCallContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.domain.models import ApprovalDecision, EffectClass, ToolDescriptor, utc_now
from harnessix.patches.batch_bridge_contracts import ManagedPatchBatchCallPlan
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from harnessix.patches.contracts import PatchProposal
from harnessix.processes.bridge_contracts import PROCESS_AGENT_FRONTENDS, AgentProcessCallPlan
from harnessix.processes.contracts import ProcessRequest
from harnessix.processes.test_contracts import RunTestsInput
from harnessix.tools.workspace import digest

READ_ONLY_POLICY_VERSION = "kernel-read-only/v1"
_TRUSTED_ACTION_NAMESPACE = UUID("f5cc2cc0-5b13-4f37-bd6e-7b3111b10c03")


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


def trusted_action_invocation_id(
    thread_id: UUID,
    turn_id: UUID,
    call: ToolCallContent,
) -> UUID:
    """从持久调用身份导出稳定Plan ID，重启不得产生第二个副作用计划。"""

    if call.tool_fingerprint is None:
        raise ValueError("Trusted Action调用缺少Tool Fingerprint")
    return uuid5(
        _TRUSTED_ACTION_NAMESPACE,
        f"{thread_id}:{turn_id}:{call.call_id}:{call.tool_fingerprint}",
    )


def trusted_action_request_fingerprint(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    *,
    plan_id: UUID,
    plan_fingerprint: str,
    execution_fingerprint: str,
    policy_id: str,
    policy_version: str,
    presentation: str,
    diff_sha256: str | None,
) -> str:
    """绑定Session归属、完整调用、Router计划及可见Review证据。"""

    return _fingerprint(
        {
            "spec_version": "harnessix.agent-trusted-action-approval/v1",
            "thread_id": str(thread.thread_id),
            "turn_id": str(turn.turn_id),
            "workspace": thread.workspace,
            "call": call.model_dump(mode="json"),
            "plan_id": str(plan_id),
            "plan_fingerprint": plan_fingerprint,
            "execution_fingerprint": execution_fingerprint,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "presentation": presentation,
            "diff_sha256": diff_sha256,
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


def approval_action_id(turn: Turn, call: ToolCallContent) -> UUID | None:
    """返回统一Action或旧Process审批绑定的稳定执行身份。"""

    item = approval_for(turn, call)
    if item is None:
        return None
    if isinstance(item.content, TrustedActionApprovalRequestContent):
        return item.content.plan_id
    if isinstance(item.content, ProcessApprovalRequestContent):
        return item.content.plan.action_id
    return None


def approval_was_decided(content: ApprovalContent, decision: ApprovalDecision) -> bool:
    """同语义决定幂等返回；冲突重放失败关闭。"""

    recorded = content.decision
    if recorded is None:
        return False
    if (recorded.outcome, recorded.actor, recorded.reason) != (
        decision.outcome,
        decision.actor,
        decision.reason,
    ):
        raise KernelError("approval_conflict", "审批已绑定其他决定")
    return True


def approval_type_matches(call: ToolCallContent, content: ApprovalContent) -> bool:
    """校验审批投影是否适用于当前Tool前端及效果类别。"""

    if not call.requires_approval:
        return False
    if isinstance(content, TrustedActionApprovalRequestContent):
        return True
    if isinstance(content, ApprovalRequestContent):
        return call.effect_class is EffectClass.READ_ONLY
    if call.effect_class is not EffectClass.NON_IDEMPOTENT_WRITE:
        return False
    if isinstance(content, ProcessApprovalRequestContent):
        return call.tool in PROCESS_AGENT_FRONTENDS
    expected_tool = (
        "apply_patch_batch"
        if isinstance(content, PatchBatchApprovalRequestContent)
        else "apply_patch"
    )
    return call.tool == expected_tool


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
    except (ValidationError, ValueError, TypeError):
        return False
    common = (
        call.tool in PROCESS_AGENT_FRONTENDS
        and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
        and call.requires_approval
        and call.tool_fingerprint is not None
        and (checked.thread_id, checked.turn_id, checked.call_id)
        == (thread.thread_id, turn.turn_id, call.call_id)
        and checked.workspace == thread.workspace
        and checked.call_fingerprint == request_fingerprint(thread, turn, call)
    )
    if not common:
        return False
    if call.tool == "run_tests":
        try:
            RunTestsInput.model_validate_json(json.dumps(call.arguments, allow_nan=False))
        except (ValidationError, ValueError, TypeError):
            return False
        return True
    try:
        process = ProcessRequest.model_validate_json(json.dumps(call.arguments, allow_nan=False))
    except (ValidationError, ValueError, TypeError):
        return False
    return (
        checked.action_tool_version == call.tool_version
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
    if isinstance(content, TrustedActionApprovalRequestContent):
        try:
            expected_plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        except ValueError:
            return False
        return (
            call.requires_approval
            and content.plan_id == expected_plan_id
            and content.request_fingerprint
            == trusted_action_request_fingerprint(
                thread,
                turn,
                call,
                plan_id=content.plan_id,
                plan_fingerprint=content.plan_fingerprint,
                execution_fingerprint=content.execution_fingerprint,
                policy_id=content.policy_id,
                policy_version=content.policy_version,
                presentation=content.presentation,
                diff_sha256=(
                    content.diff_artifact.sha256 if content.diff_artifact is not None else None
                ),
            )
        )
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
