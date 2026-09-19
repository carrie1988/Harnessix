"""把Router终态审计投影为Agent Tool Result及受控输出Artifact。"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TrustedActionEffect,
    Turn,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRouteSnapshot
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

    provider = state.outputs.get(call.tool)
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
    projected = await cancel.run(
        provider.output(
            route,
            thread,
            turn,
            call,
            expected_output_sha256=event.output_sha256,
            expected_artifact_sha256=event.artifact_sha256,
            cancel=cancel,
        )
    )
    return build_result(
        route,
        call,
        outcome.model_copy(update={"output": projected}),
        origin=origin,
        approval=approval,
    )


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
