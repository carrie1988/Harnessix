"""内联投影的超时、取消、Hash与独立副本不得改写已确认的动作事实。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.trusted_actions import agent_gateway_output, output_budget
from harnessix.trusted_actions.agent_gateway_output import terminal_result
from tests.trusted_actions.test_agent_gateway import agent_state
from tests.trusted_actions.test_builtin_success_contracts import ReturningBuiltin, builtin_route


@pytest.mark.parametrize("mode", ["timeout", "token", "parent"])
async def test_inline_projection_stops_after_validation_without_reexecution(
    tmp_path, monkeypatch, mode
):
    root = tmp_path / "workspace"
    executor = ReturningBuiltin("patch", "valid")
    actions, plans, audit, initial = builtin_route(root, "patch", executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        state = SimpleNamespace(router=actions, outputs={})
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        before = actions.events(route.plan.execution.plan_id)
        token = CancelToken()
        original = agent_gateway_output.validate_success_summary
        clock = [agent_gateway_output.monotonic()]

        def validate(plan, body):
            original(plan, body)
            if mode == "timeout":
                clock[0] += 20
            elif mode == "token":
                token.cancel()
            else:
                asyncio.current_task().cancel()

        monkeypatch.setattr(agent_gateway_output, "validate_success_summary", validate)
        monkeypatch.setattr(agent_gateway_output, "monotonic", lambda: clock[0])
        monkeypatch.setattr(output_budget, "monotonic", lambda: clock[0])
        error = (
            KernelError
            if mode == "timeout"
            else TurnCancelled
            if mode == "token"
            else asyncio.CancelledError
        )
        with pytest.raises(error) as caught:
            await asyncio.create_task(
                terminal_result(
                    state, route, thread, turn, call, outcome, token, origin="execution"
                )
            )
        if mode == "timeout":
            assert caught.value.code == "trusted_action_output_timeout"
        assert actions.events(route.plan.execution.plan_id) == before
        assert actions.status(route.plan.execution.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
        monkeypatch.undo()
        result = await terminal_result(
            state, route, thread, turn, call, outcome, CancelToken(), origin="recovery"
        )
        assert result.outcome == "succeeded" and result.output == outcome.output
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        plans.close()
        audit.close()


@pytest.mark.parametrize("case", ["digest", "state", "missing_digest"])
async def test_inline_success_requires_current_audit_event(tmp_path, monkeypatch, case):
    root = tmp_path / "workspace"
    executor = ReturningBuiltin("patch", "valid")
    actions, plans, audit, initial = builtin_route(root, "patch", executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        before = actions.events(route.plan.execution.plan_id)
        update = (
            {"to_state": "unknown"}
            if case == "state"
            else {"output_sha256": None if case == "missing_digest" else "c" * 64}
        )
        observed = (*before[:-1], before[-1].model_copy(update=update))
        monkeypatch.setattr(actions, "events", lambda _: observed)
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool": route.plan.binding.tool})
        with pytest.raises(KernelError) as caught:
            await terminal_result(
                SimpleNamespace(router=actions, outputs={}),
                route,
                thread,
                turn,
                call,
                outcome,
                CancelToken(),
                origin="execution",
            )
        assert caught.value.code == "trusted_action_output_mismatch"
        monkeypatch.undo()
        assert actions.events(route.plan.execution.plan_id) == before
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        plans.close()
        audit.close()


async def test_inline_success_returns_independent_native_json(tmp_path):
    root = tmp_path / "workspace"
    executor = ReturningBuiltin("patch", "valid")
    actions, plans, audit, initial = builtin_route(root, "patch", executor)
    try:
        outcome = await actions.execute(initial.plan.execution.plan_id)
        route = actions.status(initial.plan.execution.plan_id)
        thread, turn, call = agent_state(root)
        result = await terminal_result(
            SimpleNamespace(router=actions, outputs={}),
            route,
            thread,
            turn,
            call,
            outcome,
            CancelToken(),
            origin="execution",
        )
        expected = dict(result.output)
        outcome.output.clear()
        assert result.output == expected and result.outcome == "succeeded"
    finally:
        plans.close()
        audit.close()
