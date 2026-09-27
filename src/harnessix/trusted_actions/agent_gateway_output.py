"""把Router事实投影为Agent审批请求、Tool Result及受控输出Artifact。"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid5

from pydantic import JsonValue

from harnessix.agent.approvals import trusted_action_request_fingerprint
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TrustedActionEffect,
    Turn,
)
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRouteSnapshot
from harnessix.trusted_actions.output_budget import (
    DEFAULT_OUTPUT_BUDGET,
    bounded_projection,
    projection_checkpoint,
)
from harnessix.trusted_actions.public_errors import sanitize_gateway_exception
from harnessix.trusted_actions.public_outcomes import (
    normalize_failure_outcome,
    validate_public_projection,
)
from harnessix.trusted_actions.router import TrustedActionRouter


class TrustedActionOutputProvider(Protocol):
    """从受信Owner重建终态正文并发布与审计摘要绑定的Artifact。"""

    async def output(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        *,
        expected_output_sha256: str,
        expected_artifact_sha256: str,
        cancel: CancelToken,
    ) -> JsonValue: ...


class GatewayOutputState(Protocol):
    """输出投影只依赖Router和按Tool冻结的输出Provider。"""

    @property
    def router(self) -> TrustedActionRouter: ...

    @property
    def outputs(self) -> dict[str, TrustedActionOutputProvider]: ...


def build_approval(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    route: ActionRouteSnapshot,
    review: TrustedActionReview,
    *,
    presentation: Literal["tool", "patch_batch", "process"],
) -> TrustedActionApprovalRequestContent:
    """只投影同一Router计划与Review，不产生批准或执行副作用。"""

    diff = review.diff_artifact
    if presentation == "patch_batch" and diff is None:
        raise KernelError("trusted_action_review_missing", "Patch审批缺少Diff Artifact")
    if presentation == "process" and diff is not None:
        raise KernelError("trusted_action_review_invalid", "Process审批不能携带Diff Artifact")
    plan = route.plan
    fingerprint = trusted_action_request_fingerprint(
        thread,
        turn,
        call,
        plan_id=plan.execution.plan_id,
        plan_fingerprint=plan.fingerprint,
        execution_fingerprint=plan.execution.fingerprint,
        policy_id=plan.execution.policy.policy_id,
        policy_version=plan.execution.policy.version,
        presentation=presentation,
        diff_sha256=diff.sha256 if diff is not None else None,
    )
    return TrustedActionApprovalRequestContent(
        approval_id=uuid5(plan.execution.plan_id, "harnessix.agent-trusted-action-approval/v1"),
        call_id=call.call_id,
        presentation=presentation,
        plan_id=plan.execution.plan_id,
        plan_fingerprint=plan.fingerprint,
        execution_fingerprint=plan.execution.fingerprint,
        request_fingerprint=fingerprint,
        policy_id=plan.execution.policy.policy_id,
        policy_version=plan.execution.policy.version,
        route_state="pending_approval",
        diff_artifact=diff,
    )


async def terminal_result(
    state: GatewayOutputState,
    route: ActionRouteSnapshot,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    outcome: ActionExecutionOutcome,
    cancel: CancelToken,
    *,
    origin: Literal["execution", "recovery"],
    approval: TrustedActionApprovalRequestContent | None = None,
) -> ToolResultContent:
    """核对Router终态摘要后，由配置的Owner重建正文并发布引用。"""

    outcome = normalize_failure_outcome(route.plan, outcome, stage=None, allow_pending_output=True)
    provider = state.outputs.get(call.tool)
    if outcome.kind != "succeeded" and outcome.artifact_sha256 is None:
        return build_result(route, call, outcome, origin=origin, approval=approval)
    if provider is None or route.state == "denied":
        return build_result(route, call, outcome, origin=origin, approval=approval)
    event = state.router.events(route.plan.execution.plan_id)[-1]
    if event.output_sha256 is None and event.artifact_sha256 is None:
        return build_result(route, call, outcome, origin=origin, approval=approval)
    if (
        event.to_state != route.state
        or event.output_sha256 is None
        or event.artifact_sha256 is None
        or outcome.artifact_sha256 != event.artifact_sha256
        or (outcome.output is not None and canonical_digest(outcome.output) != event.output_sha256)
    ):
        raise KernelError("trusted_action_output_mismatch", "Action输出与Router审计终态不匹配")
    projected = await _project_output(
        provider,
        route,
        thread,
        turn,
        call,
        outcome,
        cancel,
        output_sha256=event.output_sha256,
        artifact_sha256=event.artifact_sha256,
    )
    return build_result(
        route,
        call,
        outcome.model_copy(update={"output": projected}),
        origin=origin,
        approval=approval,
    )


async def _project_output(
    provider: TrustedActionOutputProvider,
    route: ActionRouteSnapshot,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    outcome: ActionExecutionOutcome,
    cancel: CancelToken,
    *,
    output_sha256: str,
    artifact_sha256: str,
) -> JsonValue:
    """回调、序列化及正式合同共用投影时限；不改写已持久化的动作事实。"""

    budget = DEFAULT_OUTPUT_BUDGET
    deadline = monotonic() + budget.timeout_seconds
    try:
        async with asyncio.timeout(budget.timeout_seconds) as timer:
            raw = await cancel.run(
                provider.output(
                    route,
                    thread,
                    turn,
                    call,
                    expected_output_sha256=output_sha256,
                    expected_artifact_sha256=artifact_sha256,
                    cancel=cancel,
                )
            )
            projected = bounded_projection(raw, budget=budget, cancel=cancel, deadline=deadline)
            validate_public_projection(
                route.plan,
                outcome,
                projected,
                expected_output_sha256=output_sha256,
                expected_artifact_sha256=artifact_sha256,
            )
            projection_checkpoint(cancel, deadline)
            return projected
    except TurnCancelled:
        raise
    except TimeoutError as error:
        if timer.expired():
            raise KernelError("trusted_action_output_timeout", "Action输出投影超时") from None
        raise sanitize_gateway_exception(error, stage="output") from None
    except Exception as error:
        raise sanitize_gateway_exception(error, stage="output") from None


def build_result(
    route: ActionRouteSnapshot,
    call: ToolCallContent,
    outcome: ActionExecutionOutcome,
    *,
    origin: Literal["execution", "recovery"],
    approval: TrustedActionApprovalRequestContent | None = None,
) -> ToolResultContent:
    """生成不泄漏审计正文的稳定Agent结果投影。"""

    state = outcome.kind
    error = None
    if state != "succeeded":
        error = AgentFailure(
            code=outcome.error_code or "trusted_action_failed",
            message="Trusted Action未成功；详细事实请查询审计记录",
        )
    effect = TrustedActionEffect(
        plan_id=route.plan.execution.plan_id,
        plan_fingerprint=route.plan.fingerprint,
        state=state,
        origin=origin,
        artifact_sha256=outcome.artifact_sha256,
    )
    return ToolResultContent(
        call_id=call.call_id,
        outcome="unknown" if state == "manual_intervention" else state,
        output=outcome.output,
        error=error,
        action_id=effect.plan_id,
        trusted_action=effect,
        diff_artifact=approval.diff_artifact if approval is not None else None,
    )
