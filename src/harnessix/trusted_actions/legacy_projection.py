"""目录合同漂移时只观察旧custom终态效果；不复用旧权限公开正文或启动执行。"""

from __future__ import annotations

from typing import Literal, Protocol, cast

from harnessix.agent.approvals import approval_matches, trusted_action_invocation_id
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.trusted_actions.agent_gateway_output import build_result
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    TrustedToolBinding,
)
from harnessix.trusted_actions.public_outcomes import failure_family, normalize_failure_outcome
from harnessix.trusted_actions.router import TrustedActionRouter


class LegacyProjectionState(Protocol):
    """仅依赖当前目录来源和只读Router查询，不访问Executor、Owner或Secret值。"""

    @property
    def router(self) -> TrustedActionRouter: ...

    @property
    def bindings(self) -> dict[str, TrustedToolBinding]: ...


def project_legacy_terminal(
    state: LegacyProjectionState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent | None,
    cancel: CancelToken,
) -> ToolResultContent | None:
    """只承认同来源、同原调用和有效原审批的确定终态；未执行/未知状态不走兼容路径。"""

    cancel.checkpoint()
    current = state.bindings.get(call.tool)
    if current is None:
        return None
    plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
    route = state.router.status(plan_id)
    plan = route.plan
    frozen = plan.binding
    if (
        route.state not in {"succeeded", "failed"}
        or failure_family(plan) != "custom"
        or (current.source, current.source_id) != (frozen.source, frozen.source_id)
        or (call.tool, call.tool_version, call.tool_fingerprint)
        != (frozen.tool, frozen.tool_version, frozen.tool_fingerprint)
        or call.effect_class is not frozen.effect_class
        or call.arguments != plan.invocation.arguments
        or call.requires_approval
        != (
            frozen.effect_class is not EffectClass.READ_ONLY
            or frozen.risk_level is not RiskLevel.LOW
        )
    ):
        return None
    if call.requires_approval and approval is None:
        return None
    if not _legacy_approval_matches(plan, thread, turn, call, approval):
        return None
    event = state.router.events(plan_id)[-1]
    if event.to_state != route.state:
        return None
    outcome = ActionExecutionOutcome(
        kind=cast(Literal["succeeded", "failed"], route.state),
        external_action_id=plan.external_action_id,
        error_code=(event.error_code or "action_failed") if route.state == "failed" else None,
    )
    outcome = normalize_failure_outcome(plan, outcome, stage=None)
    cancel.checkpoint()
    return build_result(route, call, outcome, origin="execution", approval=approval)


def _legacy_approval_matches(
    plan: ActionRoutePlan,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent | None,
) -> bool:
    if approval is not None and (
        not approval_matches(thread, turn, call, approval)
        or approval.plan_id != plan.execution.plan_id
        or approval.plan_fingerprint != plan.fingerprint
        or approval.execution_fingerprint != plan.execution.fingerprint
        or approval.policy_id != plan.execution.policy.policy_id
        or approval.policy_version != plan.execution.policy.version
    ):
        return False
    return True
