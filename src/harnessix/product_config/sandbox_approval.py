"""本机沙箱内命令的系统自动批准（ADR 0113第2.1节）。

策略v2把这类命令标记为default.approve-sandboxed-command；Agent内核要求写调用先有批准记录，
所以由宿主以系统身份当场批准。批准前按Router持久计划再核对一次，不只看审批上的策略ID。
"""

from __future__ import annotations

from harnessix.agent.approvals import SYSTEM_APPROVAL_ACTOR_PREFIX
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.agent.runtime import ApprovalResponder
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.policy import SANDBOXED_COMMAND_POLICY_ID
from harnessix.trusted_actions.router import TrustedActionRouter

SANDBOX_APPROVAL_ACTOR = SYSTEM_APPROVAL_ACTOR_PREFIX + "sandbox-policy"
_SANDBOX_BACKENDS = frozenset({"seatbelt", "bubblewrap"})


def sandboxed_command_responder(router: TrustedActionRouter) -> ApprovalResponder:
    """只批准计划确为本机沙箱、策略确为沙箱命令且仍待批准的命令；其余一律交给人工。"""

    def respond(content: TrustedActionApprovalRequestContent) -> ApprovalDecision | None:
        if content.policy_id != SANDBOXED_COMMAND_POLICY_ID or content.presentation != "process":
            return None
        try:
            route = router.status(content.plan_id)
        except KernelError:
            return None
        execution = route.plan.execution
        if (
            route.state != "pending_approval"
            or route.plan.fingerprint != content.plan_fingerprint
            or execution.policy.policy_id != SANDBOXED_COMMAND_POLICY_ID
            or execution.sandbox.level != "host_sandboxed"
            or execution.sandbox.backend not in _SANDBOX_BACKENDS
        ):
            return None
        return ApprovalDecision(
            outcome=ApprovalOutcome.APPROVED,
            actor=SANDBOX_APPROVAL_ACTOR,
            reason="本机沙箱内的命令自动执行（ADR 0113）",
        )

    return respond
