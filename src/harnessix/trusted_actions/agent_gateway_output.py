"""把Router事实投影为Agent审批请求、Tool Result及受控输出Artifact。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
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
from harnessix.domain.models import ApprovalDecision, ToolDescriptor, utc_now
from harnessix.execution.contracts import SecretVersionBinding, canonical_digest
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRouteSnapshot
from harnessix.trusted_actions.output_budget import (
    DEFAULT_OUTPUT_BUDGET,
    bounded_projection,
    projection_checkpointer,
)
from harnessix.trusted_actions.public_errors import (
    PUBLIC_OUTPUT_REJECTIONS,
    sanitize_gateway_exception,
)
from harnessix.trusted_actions.public_outcomes import (
    failure_family,
    normalize_failure_outcome,
    public_success_schema,
    validate_public_projection,
    validate_success_summary,
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


class SecretOutputProtection(Protocol):
    """宿主注入的公开保护能力；纯接口避免Trusted Actions依赖具体Secret Provider。"""

    def assert_safe(
        self,
        value: JsonValue,
        bindings: Sequence[SecretVersionBinding],
        *,
        checkpoint: Callable[[], None],
    ) -> None: ...

    def close(self) -> None: ...


class GatewayOutputState(Protocol):
    """输出投影只依赖Router和按Tool冻结的输出Provider。"""

    @property
    def router(self) -> TrustedActionRouter: ...

    @property
    def outputs(self) -> dict[str, TrustedActionOutputProvider]: ...

    @property
    def secret_scope(self) -> SecretOutputProtection | None: ...


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


def decision_time(
    approval: TrustedActionApprovalRequestContent, decision: ApprovalDecision
) -> datetime:
    """复用原批准时间与冲突算法，和审批呈现一起维护，不扩大状态语义。"""
    recorded = approval.decision
    if recorded is None:
        return utc_now()
    if (recorded.outcome, recorded.actor, recorded.reason) != (
        decision.outcome,
        decision.actor,
        decision.reason,
    ):
        raise KernelError("approval_conflict", "Session审批已经绑定其他决定")
    return recorded.decided_at


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
    descriptor: ToolDescriptor | None = None,
    metadata_only_on_rejection: bool = False,
) -> ToolResultContent:
    """核对Router终态摘要后，由配置的Owner重建正文并发布引用。"""

    outcome = normalize_failure_outcome(route.plan, outcome, stage=None, allow_pending_output=True)
    provider = state.outputs.get(call.tool)
    if outcome.kind != "succeeded" and outcome.artifact_sha256 is None:
        return build_result(route, call, outcome, origin=origin, approval=approval)
    if provider is None or route.state == "denied":
        if outcome.kind == "succeeded" and outcome.output is not None:
            outcome = await _inline_success(state, route, outcome, cancel, descriptor)
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
    if outcome.output is None and route.plan.execution.secrets:
        # 历史Hash不能证明原Secret值；只恢复已验真的效果元数据，不授予工件或正文。
        return build_result(
            route,
            call,
            outcome.model_copy(update={"artifact_sha256": None}),
            origin=origin,
            approval=approval,
        )
    try:
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
            descriptor=descriptor,
            secret_scope=getattr(state, "secret_scope", None),
        )
    except KernelError as error:
        if not metadata_only_on_rejection or error.code not in PUBLIC_OUTPUT_REJECTIONS:
            raise
        # 原Router终态和双Hash已在上方验真；拒绝正文不等于效果未知，也不能再次执行。
        return build_result(
            route,
            call,
            outcome.model_copy(update={"output": None, "artifact_sha256": None}),
            origin=origin,
            approval=approval,
        )

    result = build_result(
        route,
        call,
        outcome.model_copy(update={"output": projected}),
        origin=origin,
        approval=approval,
    )
    return _process_failure_feedback(route, result)


def _process_failure_feedback(
    route: ActionRouteSnapshot, result: ToolResultContent
) -> ToolResultContent:
    """只消费刚通过Owner、审计Hash、Secret及DTO验证的投影；不复制日志正文。"""
    output, error = result.output, result.error
    if (
        failure_family(route.plan) != "process"
        or result.outcome != "failed"
        or error is None
        or error.code != "process_nonzero_exit"
        or not isinstance(output, dict)
        or output.get("version") != "trusted-process-output/v2"
    ):
        return result
    preview = output["diagnostic_preview"]
    assert isinstance(preview, dict)
    visible = all(
        isinstance(stream, dict) and stream["text"] is not None and not stream["truncated"]
        for stream in (preview["stdout"], preview["stderr"])
    )
    message = "检查已运行并以非零退出，不是启动失败。"
    message += (
        "diagnostic_preview已完整展示stdout/stderr诊断正文；无需仅为重复诊断读取Artifact。"
        if output["complete"] and visible
        else "diagnostic_preview不完整或不可显示；需要更多诊断时按原Artifact引用有界读取。"
    )
    return result.model_copy(update={"error": error.model_copy(update={"message": message})})


async def _inline_success(
    state: GatewayOutputState,
    route: ActionRouteSnapshot,
    outcome: ActionExecutionOutcome,
    cancel: CancelToken,
    descriptor: ToolDescriptor | None,
) -> ActionExecutionOutcome:
    """无Owner的正文仍有投影预算、审计Hash和正式摘要检查；不改写已确认效果。"""

    budget = DEFAULT_OUTPUT_BUDGET
    deadline = monotonic() + budget.timeout_seconds
    checkpoint = projection_checkpointer(cancel, deadline)
    try:
        async with asyncio.timeout(budget.timeout_seconds) as timer:
            await asyncio.sleep(0)
            projected = bounded_projection(
                outcome.output, budget=budget, cancel=cancel, deadline=deadline
            )
            event = state.router.events(route.plan.execution.plan_id)[-1]
            if event.to_state != route.state or canonical_digest(projected) != event.output_sha256:
                raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配")
            validate_success_summary(
                route.plan,
                projected,
                descriptor=descriptor,
                checkpoint=checkpoint,
            )
            _check_secret_output(
                getattr(state, "secret_scope", None),
                route,
                projected,
                checkpoint,
            )
            checkpoint()
            await asyncio.sleep(0)
            checkpoint()
            return outcome.model_copy(update={"output": projected})
    except TurnCancelled:
        raise
    except TimeoutError as error:
        if timer.expired():
            raise KernelError("trusted_action_output_timeout", "Action输出投影超时") from None
        raise sanitize_gateway_exception(error, stage="output") from None
    except Exception as error:
        raise sanitize_gateway_exception(error, stage="output") from None


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
    descriptor: ToolDescriptor | None,
    secret_scope: SecretOutputProtection | None,
) -> JsonValue:
    """回调、序列化及正式合同共用投影时限；不改写已持久化的动作事实。"""

    budget = DEFAULT_OUTPUT_BUDGET
    deadline = monotonic() + budget.timeout_seconds
    checkpoint = projection_checkpointer(cancel, deadline)
    try:
        async with asyncio.timeout(budget.timeout_seconds) as timer:
            await asyncio.sleep(0)
            if outcome.output is None and route.plan.execution.secrets:
                # 只有Hash的旧正文无法证明原Secret值；在Owner发布前拒绝恢复正文。
                raise KernelError(
                    "trusted_action_secret_unavailable", "Action输出缺少匹配的Secret保护能力"
                )
            if outcome.kind == "succeeded":
                # 即使恢复没有原正文，也不允许无公开合同的Owner先发布工件。
                public_success_schema(route.plan, descriptor)
            if outcome.output is not None:
                # 正式摘要在Owner发布工件之前验证；恢复缺少正文时再核对Owner重建值。
                preview = bounded_projection(
                    outcome.output, budget=budget, cancel=cancel, deadline=deadline
                )
                _check_secret_output(secret_scope, route, preview, checkpoint)
                if outcome.kind == "succeeded":
                    validate_success_summary(
                        route.plan,
                        preview,
                        descriptor=descriptor,
                        checkpoint=checkpoint,
                    )
                checkpoint()
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
            _check_secret_output(secret_scope, route, projected, checkpoint)
            validate_public_projection(
                route.plan,
                outcome,
                projected,
                expected_output_sha256=output_sha256,
                expected_artifact_sha256=artifact_sha256,
                descriptor=descriptor,
                checkpoint=checkpoint,
            )
            checkpoint()
            return projected
    except TurnCancelled:
        raise
    except TimeoutError as error:
        if timer.expired():
            raise KernelError("trusted_action_output_timeout", "Action输出投影超时") from None
        raise sanitize_gateway_exception(error, stage="output") from None
    except Exception as error:
        raise sanitize_gateway_exception(error, stage="output") from None


def _check_secret_output(
    scope: SecretOutputProtection | None,
    route: ActionRouteSnapshot,
    value: JsonValue,
    checkpoint: Callable[[], None],
) -> None:
    """未配置能力的Secret绑定正文默认拒绝；显式宿主快照同时扫描键和值。"""
    checkpoint()
    if scope is not None:
        scope.assert_safe(value, route.plan.execution.secrets, checkpoint=checkpoint)
    elif route.plan.execution.secrets:
        raise KernelError("trusted_action_secret_unavailable", "Action输出缺少匹配的Secret保护能力")
    checkpoint()


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
