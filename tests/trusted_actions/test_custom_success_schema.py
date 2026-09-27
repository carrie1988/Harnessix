"""声明的custom合同绑定完整Tool指纹，正常/Owner/恢复不能接受额外字段。"""

from __future__ import annotations

import copy

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from tests.domain.test_public_output_schema import rich_schema
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FixedOutput,
    agent_state,
    build_gateway,
    descriptor,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload
from tests.trusted_actions.test_success_projection_boundaries import output_reference


@pytest.mark.parametrize("delivery", ["inline", "owner", "recovery"])
@pytest.mark.parametrize(
    "case", ["valid", "extra", "nested_extra", "type", "missing", "fingerprint"]
)
async def test_declared_contract_and_original_hash_are_both_required(tmp_path, delivery, case):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    tool = descriptor().model_copy(update={"public_output_schema": rich_schema()})
    body = {"count": 3, "message": None, "items": [{"ok": True}], "tag": "pass"}
    if case == "extra":
        body["diagnostic"] = _payload()
    elif case == "nested_extra":
        body["items"][0]["diagnostic"] = _payload()
    elif case == "type":
        body["count"] = True
    elif case == "missing":
        del body["count"]
    original = copy.deepcopy(body)
    owner = delivery != "inline"
    provider = FixedOutput({**body, "artifact": output_reference("b" * 64)})
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded", output=body, artifact_sha256="b" * 64 if owner else None
        )
    )
    gateway, actions, plans, audit = build_gateway(
        root,
        executor,
        tool=tool,
        presentation="process" if owner else "tool",
        output=provider if owner else None,
    )
    try:
        thread, turn, call = agent_state(root)
        call = call.model_copy(update={"tool_fingerprint": tool_fingerprint(tool)})
        request = await gateway.prepare(thread, turn, call, CancelToken())
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="contract-test"),
        )
        if delivery == "recovery":
            await actions.execute(request.plan_id)
        if case == "fingerprint":
            gateway._state.definitions[tool.name].public_output_schema["title"] = "changed"
            # 用内核投影直接检查已确认计划，排除更早的调用身份校验掩盖本边界。
            if delivery != "recovery":
                await actions.execute(request.plan_id)
            from harnessix.trusted_actions.agent_gateway_output import terminal_result

            operation = terminal_result(
                gateway._state,
                actions.status(request.plan_id),
                thread,
                turn,
                call,
                executor.outcome,
                CancelToken(),
                origin="execution",
                descriptor=gateway._state.definitions[tool.name],
            )
        else:
            operation = (
                gateway.recover(thread, turn, call, approved, CancelToken())
                if delivery == "recovery"
                else gateway.execute(thread, turn, call, approved, CancelToken())
            )
        if case == "valid":
            result = await operation
            assert result.output == (
                {**original, "artifact": provider.projected["artifact"]} if owner else original
            )
        else:
            with pytest.raises(KernelError) as caught:
                await operation
            assert caught.value.code == "trusted_action_output_mismatch"
            _assert_no_leak(str(caught.value))
        assert actions.status(request.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
        assert provider.calls == int(
            owner and (case == "valid" or (delivery == "recovery" and case != "fingerprint"))
        )
        assert body == original
    finally:
        audit.close()
        plans.close()


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("terminal", [True, False])
async def test_contract_change_can_only_observe_original_confirmed_effect(
    tmp_path, read_only, terminal
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    old = descriptor()
    if read_only:
        old = old.model_copy(
            update={
                "effect_class": EffectClass.READ_ONLY,
                "risk_level": RiskLevel.LOW,
                "requires_idempotency": False,
                "requires_approval": False,
                "supports_reconciliation": False,
            }
        )
    first = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"summary": "done"}))
    gateway, actions, plans, audit = build_gateway(root, first, tool=old)
    thread, turn, call = agent_state(root)
    call = call.model_copy(
        update={
            "tool_fingerprint": tool_fingerprint(old),
            "effect_class": old.effect_class,
            "requires_approval": old.requires_approval,
        }
    )
    from harnessix.trusted_actions.agent_gateway_support import _build_invocation

    route = actions.plan(
        _build_invocation(gateway._state, thread, turn, call, gateway._state.bindings[call.tool]),
        gateway._state.context(thread, turn, call),
    )
    approved = None
    if not read_only:
        request = await gateway.prepare(thread, turn, call, CancelToken())
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="migration-test"),
        )
    if terminal:
        await actions.execute(route.plan.execution.plan_id)
    before = actions.events(route.plan.execution.plan_id)
    gateway.close()
    audit.close()
    plans.close()
    new = old.model_copy(update={"public_output_schema": rich_schema()})
    second = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    replacement, reopened, plans, audit = build_gateway(root, second, tool=new)
    try:
        if terminal:
            result = await replacement.recover(thread, turn, call, approved, CancelToken())
            assert result.output is None and result.trusted_action.state == "succeeded"
            assert result.trusted_action.plan_fingerprint == route.plan.fingerprint
        else:
            with pytest.raises(KernelError) as caught:
                await replacement.recover(thread, turn, call, approved, CancelToken())
            assert caught.value.code == "trusted_tool_contract_changed"
        assert reopened.events(route.plan.execution.plan_id) == before
        assert second.calls == second.reconciliations == 0
        assert first.calls == int(terminal)
    finally:
        audit.close()
        plans.close()


@pytest.mark.parametrize("mode", ["timeout", "token", "parent"])
async def test_custom_validation_stays_inside_gateway_deadline_and_cancellation(
    tmp_path, monkeypatch, mode
):
    import asyncio

    import harnessix.domain.public_output_schema as contracts
    from harnessix.agent.cancellation import TurnCancelled
    from harnessix.trusted_actions import agent_gateway_output, output_budget

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged")
    tool = descriptor()
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"summary": "public"}))
    gateway, actions, plans, audit = build_gateway(root, executor, tool=tool)
    token = CancelToken()
    clock = [agent_gateway_output.monotonic()]
    original = contracts._Work.step

    def step(work):
        if mode == "timeout":
            clock[0] += 20
        elif mode == "token":
            token.cancel()
        else:
            asyncio.current_task().cancel()
        original(work)

    monkeypatch.setattr(contracts._Work, "step", step)
    monkeypatch.setattr(agent_gateway_output, "monotonic", lambda: clock[0])
    monkeypatch.setattr(output_budget, "monotonic", lambda: clock[0])
    try:
        thread, turn, call = agent_state(root)
        request = await gateway.prepare(thread, turn, call, CancelToken())
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="lifecycle-test"),
        )
        error = (
            KernelError
            if mode == "timeout"
            else TurnCancelled
            if mode == "token"
            else asyncio.CancelledError
        )
        with pytest.raises(error) as caught:
            await asyncio.create_task(gateway.execute(thread, turn, call, approved, token))
        if mode == "timeout":
            assert caught.value.code == "trusted_action_output_timeout"
        assert actions.status(request.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        audit.close()
        plans.close()
