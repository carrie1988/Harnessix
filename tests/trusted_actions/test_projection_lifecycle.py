"""Provider投影有界退出、子任务回收及查询优先恢复不重复动作。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions import agent_gateway_output
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.output_budget import ActionOutputBudget
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FixedOutput,
    agent_state,
    build_gateway,
)
from tests.trusted_actions.test_success_projection_boundaries import output_reference


class BlockedOutput:
    """用进入/退出握手验证清理，不用固定sleep推测子任务已经运行。"""

    def __init__(self):
        self.entered, self.closed = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.recovered = None

    async def output(self, *_args, **_kwargs):
        self.calls += 1
        if self.recovered is not None:
            return self.recovered
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed.set()


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("mode", ["timeout", "token", "parent"])
async def test_projection_deadline_and_cancel_collect_child_without_reexecution(
    tmp_path: Path, monkeypatch, recovery, mode
):
    root = tmp_path / "workspace"
    root.mkdir()
    provider = BlockedOutput()
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded", output={"summary": "completed"}, artifact_sha256="b" * 64
        )
    )
    gateway, actions, plans, audit = build_gateway(
        root, executor, presentation="process", output=provider
    )
    monkeypatch.setattr(
        agent_gateway_output,
        "DEFAULT_OUTPUT_BUDGET",
        ActionOutputBudget(timeout_seconds=0.1 if mode == "timeout" else 5.0),
    )
    token = CancelToken()
    try:
        thread, turn, call = agent_state(root)
        request = await gateway.prepare(thread, turn, call, token)
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="lifecycle-test"),
        )
        if recovery:
            await actions.execute(request.plan_id)
        task = asyncio.create_task(
            (gateway.recover if recovery else gateway.execute)(thread, turn, call, approved, token)
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        if mode == "token":
            token.cancel()
        elif mode == "parent":
            task.cancel()
        expected = (
            KernelError
            if mode == "timeout"
            else TurnCancelled
            if mode == "token"
            else asyncio.CancelledError
        )
        with pytest.raises(expected) as caught:
            await task
        if mode == "timeout":
            assert caught.value.code == "trusted_action_output_timeout"
        assert provider.closed.is_set() and provider.calls == 1
        assert actions.status(request.plan_id).state == "succeeded"
        assert actions.events(request.plan_id)[-1].error_code is None
        assert executor.calls == 1 and executor.reconciliations == 0
        # 未保存Tool Result时可重新查询Owner，但不能重执行或重对账。
        provider.recovered = {"summary": "completed", "artifact": output_reference("b" * 64)}
        before = actions.events(request.plan_id)
        result = await gateway.recover(thread, turn, call, approved, CancelToken())
        assert result.outcome == "succeeded" and actions.events(request.plan_id) == before
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("case", ["bytes", "nodes", "depth", "int", "cycle"])
async def test_projection_resource_limit_preserves_terminal_audit(tmp_path: Path, recovery, case):
    root = tmp_path / "workspace"
    root.mkdir()
    value = {"summary": "completed", "artifact": output_reference("b" * 64)}
    if case == "bytes":
        value["body"] = "x" * (1024 * 1024 + 1)
    elif case == "nodes":
        value["body"] = [None] * 20000
    elif case == "depth":
        body = []
        for _ in range(65):
            body = [body]
        value["body"] = body
    elif case == "int":
        value["body"] = 1 << 200
    else:
        value["body"] = value
    executor = FakeExecutor(
        ActionExecutionOutcome(
            kind="succeeded", output={"summary": "completed"}, artifact_sha256="b" * 64
        )
    )
    gateway, actions, plans, audit = build_gateway(
        root, executor, presentation="process", output=FixedOutput(value)
    )
    try:
        thread, turn, call = agent_state(root)
        request = await gateway.prepare(thread, turn, call, CancelToken())
        approved = gateway.decide(
            thread,
            turn,
            call,
            request,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="budget-test"),
        )
        if recovery:
            await actions.execute(request.plan_id)
        with pytest.raises(KernelError) as caught:
            await (gateway.recover if recovery else gateway.execute)(
                thread, turn, call, approved, CancelToken()
            )
        assert caught.value.code == (
            "trusted_action_output_mismatch" if case == "cycle" else "trusted_action_output_limit"
        )
        assert actions.status(request.plan_id).state == "succeeded"
        assert executor.calls == 1 and executor.reconciliations == 0
    finally:
        gateway.close()
        plans.close()
        audit.close()
