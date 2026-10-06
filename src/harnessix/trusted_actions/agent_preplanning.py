"""Agent 首次 Route 前的可信异步准备；复用原规范化、查询、策略和真实捕获。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.domain.models import EffectClass
from harnessix.trusted_actions.agent_gateway_invocation import build_agent_action_invocation
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, CodingActionInvocation
from harnessix.trusted_actions.planning import (
    _load_existing_route,
    _normalize_invocation,
    _plan_normalized_action,
)
from harnessix.trusted_actions.prepared_resolution import (
    snapshot_prepared_resolution,
    validate_prepared_route_resolution,
)
from harnessix.trusted_actions.public_errors import sanitize_plan_exception
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter


async def plan_agent_action(
    router: TrustedActionRouter,
    invocation: CodingActionInvocation,
    context: ActionPlanningContext,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
) -> ActionRouteSnapshot:
    """已有 Route 不重新生成意图；只让注册准备器参与首次正式规划。"""
    definition = router._definition(invocation.source, invocation.source_id, invocation.tool)
    if definition.agent_prepare is None:
        return router.plan(invocation, context)

    callback_error: BaseException | None = None

    def check() -> None:
        nonlocal callback_error
        try:
            if context.checkpoint is not None:
                context.checkpoint()
            cancel.checkpoint()
        except BaseException as error:
            callback_error = error
            raise

    check()
    expected = build_agent_action_invocation(
        thread,
        turn,
        call,
        definition.binding,
        requires_idempotency=definition.binding.effect_class
        in {
            EffectClass.NON_IDEMPOTENT_WRITE,
            EffectClass.DESTRUCTIVE,
        },
    )
    if invocation != expected:
        raise KernelError("trusted_action_plan_mismatch", "Router计划与Agent调用不匹配")
    original = CodingActionInvocation.model_validate_json(invocation.model_dump_json())
    checked, arguments = _normalize_invocation(original, definition)
    existing = _load_existing_route(router._audit, checked, definition.binding)
    if existing is not None:
        check()
        router._plans.save_plan(existing.plan.execution)
        check()
        return existing
    # 私有观察前先满足原非幂等调用约束，不能先运行准备再由同步路径拒绝。
    if (
        definition.binding.effect_class
        in {EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.DESTRUCTIVE}
        and checked.idempotency_key is None
    ):
        raise KernelError("idempotency_key_required", "该Action必须携带幂等键")
    try:
        # 使用原 Router 操作上限兜住等待；具体宿主仍消费其原更短绝对期限。
        async with asyncio.timeout(router._execute_timeout_seconds):
            result = await cancel.run(
                definition.agent_prepare.prepare(
                    checked.model_copy(deep=True),
                    arguments.model_copy(deep=True),
                    replace(context, checkpoint=check),
                    thread.model_copy(deep=True),
                    turn.model_copy(deep=True),
                    call.model_copy(deep=True),
                    cancel,
                )
            )
    except TurnCancelled:
        raise
    except Exception as error:
        if error is callback_error:
            raise
        raise sanitize_plan_exception(error, stage="preparation") from None
    # 检查点在错误映射外执行，取消或原绝对期限异常保留原身份。
    prepared = snapshot_prepared_resolution(result, checkpoint=check)
    check()
    route = _plan_normalized_action(
        checked,
        arguments,
        context,
        definition,
        policy=router._policy,
        plans=router._plans,
        audit=router._audit,
        prepared=prepared,
    )
    # 并发期间另一 owner 可先落库；旧 Route 永远不能被新观察静默替换。
    validate_prepared_route_resolution(route, prepared, context)
    check()
    return route
