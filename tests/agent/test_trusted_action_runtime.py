from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.models import (
    ItemStatus,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.router import TrustedActionRouter
from tests.agent.helpers import answer
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    build_gateway,
    descriptor,
    runtime_context,
)


def action_step() -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="action-response"),
        ToolCallCompleted(
            call_id="action-call",
            tool="workspace.patch",
            arguments={"path": "file.txt"},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def approval(turn: Turn) -> TrustedActionApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    )


async def test_agent_runtime_uses_gateway_for_approval_execution_and_result(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"summary": "changed"}))
    gateway, router, plans, audit = build_gateway(root, executor)
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("完成")])

    async with AgentRuntime(store, provider, trusted_actions=gateway) as runtime:
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改文件", request_id="action")
        request = approval(waiting)
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert request.route_state == "pending_approval"
        assert executor.calls == 0

        decided = await runtime.reply_approval(
            thread.thread_id,
            waiting.turn_id,
            request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=ApprovalDecision(
                outcome=ApprovalOutcome.APPROVED,
                actor="reviewer",
            ),
        )
        assert decided.status is TurnStatus.WAITING_APPROVAL
        assert router.status(request.plan_id).state == "ready"
        completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)

    results = [
        item.content for item in completed.items if isinstance(item.content, ToolResultContent)
    ]
    assert completed.status is TurnStatus.COMPLETED
    assert len(results) == 1 and results[0].trusted_action is not None
    assert results[0].trusted_action.plan_id == request.plan_id
    assert executor.calls == 1
    assert not pending_calls(completed)
    assert all(item.status is not ItemStatus.STARTED for item in completed.items)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
    plans.close()
    audit.close()


async def test_runtime_recovers_router_first_approval_crash_without_second_prompt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before", encoding="utf-8")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    first_gateway, router, plans, audit = build_gateway(root, executor)
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step()])
    injected = False

    def fault(point: str) -> None:
        nonlocal injected
        if point == "runtime.after_trusted_action_decision" and not injected:
            injected = True
            raise RuntimeError("injected crash")

    async with AgentRuntime(
        store,
        provider,
        trusted_actions=first_gateway,
        fault=fault,
    ) as runtime:
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改文件", request_id="crash")
        request = approval(waiting)
        with pytest.raises(RuntimeError, match="injected crash"):
            await runtime.reply_approval(
                thread.thread_id,
                waiting.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="reviewer",
                ),
            )

    assert router.status(request.plan_id).state == "ready"
    assert approval((await store.get_thread(thread.thread_id)).turns[-1]).decision is None

    second_gateway = build_gateway_from_existing(root, router)
    resumed_provider = ScriptedProvider([action_step(), answer("恢复完成")])
    async with AgentRuntime(
        store,
        resumed_provider,
        trusted_actions=second_gateway,
    ) as runtime:
        recovered = (await store.get_thread(thread.thread_id)).turns[-1]
        recovered_approval = approval(recovered)
        assert recovered_approval.decision is not None
        completed = await runtime.resume_turn(thread.thread_id, recovered.turn_id)

    assert completed.status is TurnStatus.COMPLETED
    assert executor.calls == 1
    assert (
        len(
            [
                item
                for item in completed.items
                if isinstance(item.content, TrustedActionApprovalRequestContent)
            ]
        )
        == 1
    )
    plans.close()
    audit.close()


def build_gateway_from_existing(
    root: Path, router: TrustedActionRouter
) -> RouterBackedAgentActionGateway:
    """复用同一Router Stores构造重启后的新Gateway，不重复注册定义。"""

    return RouterBackedAgentActionGateway(
        router,
        (descriptor(),),
        lambda *_: runtime_context(root),
    )
