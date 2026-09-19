"""Router-backed Agent Gateway的函数式校验、规划、执行和恢复核心。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import uuid5

from harnessix.agent.approvals import (
    approval_matches,
    tool_fingerprint,
    trusted_action_invocation_id,
    trusted_action_request_fingerprint,
)
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Thread,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    ToolDescriptor,
    utc_now,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.agent_gateway_output import (
    TrustedActionOutputProvider,
    terminal_result,
)
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRouteSnapshot,
    CodingActionInvocation,
    TrustedToolBinding,
)
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter

type TrustedActionPresentation = Literal["tool", "patch_batch", "process"]
type PlanningContextFactory = Callable[[Thread, Turn, ToolCallContent], ActionPlanningContext]


class TrustedActionReviewProvider(Protocol):
    """在请求人工批准前发布同一计划的有界Review Artifact。"""

    async def review(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> TrustedActionReview: ...


@dataclass(frozen=True, slots=True)
class AgentActionGatewayState:
    """Gateway构造后不再变化的Router绑定与呈现配置。"""

    router: TrustedActionRouter
    definitions: dict[str, ToolDescriptor]
    bindings: dict[str, TrustedToolBinding]
    context: PlanningContextFactory
    presentations: dict[str, TrustedActionPresentation]
    reviews: dict[str, TrustedActionReviewProvider]
    outputs: dict[str, TrustedActionOutputProvider]


def build_gateway_state(
    router: TrustedActionRouter,
    definitions: tuple[ToolDescriptor, ...],
    context: PlanningContextFactory,
    *,
    source: str,
    source_id: str,
    presentations: Mapping[str, TrustedActionPresentation] | None,
    reviews: TrustedActionReviewProvider | Mapping[str, TrustedActionReviewProvider] | None,
    outputs: Mapping[str, TrustedActionOutputProvider] | None,
) -> AgentActionGatewayState:
    copied = tuple(item.model_copy(deep=True) for item in definitions)
    if len({item.name for item in copied}) != len(copied):
        raise KernelError("trusted_action_gateway_invalid", "Gateway Tool名称重复")
    requested = dict(presentations or {})
    if set(requested) - {item.name for item in copied}:
        raise KernelError("trusted_action_gateway_invalid", "Gateway呈现包含未知Tool")
    output_providers = dict(outputs or {})
    if set(output_providers) - {item.name for item in copied}:
        raise KernelError("trusted_action_gateway_invalid", "Gateway输出包含未知Tool")
    if any(requested.get(name, "tool") != "process" for name in output_providers):
        raise KernelError("trusted_action_gateway_invalid", "Gateway输出只适用于Process呈现")
    if reviews is None:
        review_providers: dict[str, TrustedActionReviewProvider] = {}
    elif isinstance(reviews, Mapping):
        review_providers = dict(reviews)
    else:
        if len(copied) != 1:
            raise KernelError(
                "trusted_action_gateway_invalid",
                "多Tool Gateway必须显式按Tool绑定Review",
            )
        # 保留既有单Tool调用方兼容性，避免把Review静默扩散给其他Action。
        review_providers = {item.name: reviews for item in copied}
    if set(review_providers) - {item.name for item in copied}:
        raise KernelError("trusted_action_gateway_invalid", "Gateway Review包含未知Tool")
    catalog = {item.name: item for item in copied}
    bindings = _validate_bindings(router, catalog, source, source_id)
    return AgentActionGatewayState(
        router=router,
        definitions=catalog,
        bindings=bindings,
        context=context,
        presentations={item.name: requested.get(item.name, "tool") for item in copied},
        reviews=review_providers,
        outputs=output_providers,
    )


def gateway_definitions(state: AgentActionGatewayState) -> tuple[ToolDescriptor, ...]:
    return tuple(
        state.definitions[name].model_copy(deep=True) for name in sorted(state.definitions)
    )


async def prepare_action(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
) -> TrustedActionApprovalRequestContent | ToolResultContent:
    """查询优先地规划；Policy允许时执行，要求审批时只发布投影。"""

    cancel.checkpoint()
    binding = _validate_call(state, call)
    invocation = _build_invocation(state, thread, turn, call, binding)
    route = state.router.plan(invocation, state.context(thread, turn, call))
    _validate_route(route, thread, turn, call, binding)
    if route.state == "pending_approval":
        review = TrustedActionReview()
        provider = state.reviews.get(call.tool)
        if provider is not None:
            review = await cancel.run(provider.review(route, thread, turn, call, cancel))
        return _build_approval(state, thread, turn, call, route, review)
    if route.state == "ready":
        return await _execute_ready(state, route, thread, turn, call, cancel, origin="execution")
    return await _project_status(state, route, thread, turn, call, cancel, origin="execution")


def decide_action(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent,
    decision: ApprovalDecision,
) -> TrustedActionApprovalRequestContent:
    """先提交Router批准检查点，再返回使用同一时间戳的Session投影。"""

    _validate_approval(state, thread, turn, call, approval)
    checked = ApprovalDecision.model_validate_json(decision.model_dump_json())
    decided_at = _decision_time(approval, checked)
    route = state.router.decide(approval.plan_id, checked, decided_at=decided_at)
    checkpoint = state.router.approval(approval.plan_id)
    if checkpoint is None:
        raise KernelError("approval_checkpoint_missing", "Router审批检查点缺失")
    return _decision_projection(approval, route, checkpoint.decision)


def sync_action_decision(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent,
) -> TrustedActionApprovalRequestContent | None:
    """用Router已有检查点补齐崩溃前未写入的Session决定。"""

    _validate_approval(state, thread, turn, call, approval)
    checkpoint = state.router.approval(approval.plan_id)
    if checkpoint is None:
        return None
    decision = ApprovalDecision(
        outcome=checkpoint.decision.outcome,
        actor=checkpoint.decision.actor,
        reason=checkpoint.decision.reason,
    )
    route = state.router.decide(
        approval.plan_id,
        decision,
        decided_at=checkpoint.decision.decided_at,
    )
    return _decision_projection(approval, route, checkpoint.decision)


async def execute_action(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent,
    cancel: CancelToken,
) -> ToolResultContent:
    """核对Session决定并只经Router执行；UNKNOWN只能执行Reconcile。"""

    _validate_approval(state, thread, turn, call, approval)
    decision = approval.decision
    if decision is None:
        raise KernelError("approval_missing", "Trusted Action执行缺少审批决定")
    route = state.router.decide(
        approval.plan_id,
        ApprovalDecision(
            outcome=decision.outcome,
            actor=decision.actor,
            reason=decision.reason,
        ),
        decided_at=decision.decided_at,
    )
    if decision.outcome is ApprovalOutcome.REJECTED:
        return await _project_status(
            state,
            route,
            thread,
            turn,
            call,
            cancel,
            origin="execution",
            approval=approval,
        )
    if route.state == "ready":
        return await _execute_ready(
            state, route, thread, turn, call, cancel, origin="execution", approval=approval
        )
    if route.state in {"running", "reconciling"}:
        route = state.router.recover_interrupted_plan(approval.plan_id)
    if route.state == "unknown":
        cancel.checkpoint()
        outcome = await cancel.run(state.router.reconcile(approval.plan_id))
        route = state.router.status(approval.plan_id)
        return await terminal_result(
            state,
            route,
            thread,
            turn,
            call,
            outcome,
            cancel,
            origin="recovery",
            approval=approval,
        )
    return await _project_status(
        state,
        route,
        thread,
        turn,
        call,
        cancel,
        origin="recovery",
        approval=approval,
    )


async def recover_action(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent | None,
    cancel: CancelToken,
) -> ToolResultContent | None:
    """观察或核对旧计划；pending/ready不在终结路径中启动副作用。"""

    binding = _validate_call(state, call)
    plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
    try:
        route = state.router.status(plan_id)
    except KernelError as error:
        if error.code == "action_route_not_found":
            return None
        raise
    _validate_route(route, thread, turn, call, binding)
    if approval is not None:
        _validate_approval(state, thread, turn, call, approval)
        route = _restore_session_decision(state, route, approval)
    if route.state in {"pending_approval", "ready"}:
        return None
    if route.state in {"running", "reconciling"}:
        route = state.router.recover_interrupted_plan(plan_id)
    if route.state == "unknown":
        cancel.checkpoint()
        outcome = await cancel.run(state.router.reconcile(plan_id))
        route = state.router.status(plan_id)
        return await terminal_result(
            state,
            route,
            thread,
            turn,
            call,
            outcome,
            cancel,
            origin="recovery",
            approval=approval,
        )
    return await _project_status(
        state,
        route,
        thread,
        turn,
        call,
        cancel,
        origin="recovery",
        approval=approval,
    )


def _validate_bindings(
    router: TrustedActionRouter,
    definitions: dict[str, ToolDescriptor],
    source: str,
    source_id: str,
) -> dict[str, TrustedToolBinding]:
    bindings = {item.tool: item for item in router.bindings(source=source, source_id=source_id)}
    if set(bindings) != set(definitions):
        raise KernelError("trusted_action_gateway_mismatch", "Gateway目录与Router绑定不一致")
    for name, descriptor in definitions.items():
        binding = bindings[name]
        expected_idempotency = binding.effect_class in {
            EffectClass.NON_IDEMPOTENT_WRITE,
            EffectClass.DESTRUCTIVE,
        }
        expected_approval = (
            binding.effect_class is not EffectClass.READ_ONLY or binding.risk_level.value != "low"
        )
        if (
            binding.tool_version != descriptor.version
            or binding.tool_fingerprint != tool_fingerprint(descriptor)
            or binding.input_schema_sha256 != canonical_digest(descriptor.input_schema)
            or binding.effect_class is not descriptor.effect_class
            or binding.risk_level is not descriptor.risk_level
            or descriptor.requires_idempotency != expected_idempotency
            or descriptor.requires_approval != expected_approval
            or descriptor.supports_reconciliation != (binding.recovery_mode != "none")
        ):
            raise KernelError("trusted_action_gateway_mismatch", "Gateway描述与Router合同不一致")
    return bindings


def _validate_call(state: AgentActionGatewayState, call: ToolCallContent) -> TrustedToolBinding:
    definition = state.definitions.get(call.tool)
    binding = state.bindings.get(call.tool)
    if definition is None or binding is None:
        raise KernelError("trusted_action_not_registered", "调用不属于Gateway目录")
    if (
        call.tool_version != definition.version
        or call.tool_fingerprint != tool_fingerprint(definition)
        or call.effect_class is not definition.effect_class
        or call.requires_approval != definition.requires_approval
    ):
        raise KernelError("trusted_tool_contract_changed", "调用与Gateway Tool合同不一致")
    return binding


def _build_invocation(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    binding: TrustedToolBinding,
) -> CodingActionInvocation:
    plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
    idempotency_key = None
    if state.definitions[call.tool].requires_idempotency:
        idempotency_key = canonical_digest(
            {
                "spec_version": "harnessix.agent-trusted-action-idempotency/v1",
                "thread_id": str(thread.thread_id),
                "turn_id": str(turn.turn_id),
                "call_id": str(call.call_id),
                "tool_fingerprint": call.tool_fingerprint,
            }
        )
    return CodingActionInvocation(
        invocation_id=plan_id,
        source=binding.source,
        source_id=binding.source_id,
        tool=binding.tool,
        tool_version=binding.tool_version,
        tool_fingerprint=binding.tool_fingerprint,
        arguments=call.arguments,
        idempotency_key=idempotency_key,
    )


def _build_approval(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    route: ActionRouteSnapshot,
    review: TrustedActionReview,
) -> TrustedActionApprovalRequestContent:
    presentation = state.presentations[call.tool]
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


def _decision_time(
    approval: TrustedActionApprovalRequestContent, decision: ApprovalDecision
) -> datetime:
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


def _decision_projection(
    approval: TrustedActionApprovalRequestContent,
    route: ActionRouteSnapshot,
    router_record: ApprovalRecord,
) -> TrustedActionApprovalRequestContent:
    projected = ApprovalRecord(
        outcome=router_record.outcome,
        actor=router_record.actor,
        reason=router_record.reason,
        request_fingerprint=approval.request_fingerprint,
        decided_at=router_record.decided_at,
    )
    return approval.model_copy(update={"decision": projected, "route_state": route.state})


def _validate_approval(
    state: AgentActionGatewayState,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    approval: TrustedActionApprovalRequestContent,
) -> ActionRouteSnapshot:
    binding = _validate_call(state, call)
    if not approval_matches(thread, turn, call, approval):
        raise KernelError("approval_mismatch", "Trusted Action审批与调用不匹配")
    route = state.router.status(approval.plan_id)
    _validate_route(route, thread, turn, call, binding)
    plan = route.plan
    if (
        approval.plan_fingerprint != plan.fingerprint
        or approval.execution_fingerprint != plan.execution.fingerprint
        or approval.policy_id != plan.execution.policy.policy_id
        or approval.policy_version != plan.execution.policy.version
        or approval.presentation != state.presentations[call.tool]
    ):
        raise KernelError("approval_mismatch", "Trusted Action审批与Router计划不匹配")
    return route


def _validate_route(
    route: ActionRouteSnapshot,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    binding: TrustedToolBinding,
) -> None:
    plan = route.plan
    if (
        plan.execution.plan_id != trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        or plan.binding != binding
        or plan.invocation.tool != call.tool
        or plan.invocation.tool_version != call.tool_version
        or plan.invocation.tool_fingerprint != call.tool_fingerprint
    ):
        raise KernelError("trusted_action_plan_mismatch", "Router计划与Agent调用不匹配")


def _restore_session_decision(
    state: AgentActionGatewayState,
    route: ActionRouteSnapshot,
    approval: TrustedActionApprovalRequestContent,
) -> ActionRouteSnapshot:
    decision = approval.decision
    if decision is None or route.state != "pending_approval":
        return route
    return state.router.decide(
        approval.plan_id,
        ApprovalDecision(
            outcome=decision.outcome,
            actor=decision.actor,
            reason=decision.reason,
        ),
        decided_at=decision.decided_at,
    )


async def _execute_ready(
    state: AgentActionGatewayState,
    route: ActionRouteSnapshot,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
    *,
    origin: Literal["execution", "recovery"],
    approval: TrustedActionApprovalRequestContent | None = None,
) -> ToolResultContent:
    cancel.checkpoint()
    outcome = await cancel.run(state.router.execute(route.plan.execution.plan_id))
    route = state.router.status(route.plan.execution.plan_id)
    return await terminal_result(
        state,
        route,
        thread,
        turn,
        call,
        outcome,
        cancel,
        origin=origin,
        approval=approval,
    )


async def _project_status(
    state: AgentActionGatewayState,
    route: ActionRouteSnapshot,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
    *,
    origin: Literal["execution", "recovery"],
    approval: TrustedActionApprovalRequestContent | None = None,
) -> ToolResultContent:
    if route.state not in {"denied", "succeeded", "failed", "unknown", "manual_intervention"}:
        raise KernelError("trusted_action_not_terminal", "Trusted Action尚未形成可投影终态")
    event = state.router.events(route.plan.execution.plan_id)[-1]
    outcome_kind = cast(
        Literal["succeeded", "failed", "unknown", "manual_intervention"],
        "failed" if route.state == "denied" else route.state,
    )
    outcome = ActionExecutionOutcome(
        kind=outcome_kind,
        artifact_sha256=event.artifact_sha256,
        external_action_id=route.plan.external_action_id,
        error_code=(event.error_code or "action_failed") if route.state != "succeeded" else None,
    )
    return await terminal_result(
        state,
        route,
        thread,
        turn,
        call,
        outcome,
        cancel,
        origin=origin,
        approval=approval,
    )
