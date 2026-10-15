"""预算截断归类：剩余预算压低的输出上限被用满才算预算耗尽，截断提案从不执行。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, ToolCallContent, TurnStatus, Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.agent.test_provider_input_budget import WireProvider


def truncated(output_tokens: int):
    return [
        ResponseStarted(response_id="truncated"),
        ToolCallCompleted(call_id="write", tool="test.read", arguments={}),
        ResponseCompleted(
            finish_reason="max_output_tokens",
            usage=Usage(input_tokens=10, output_tokens=output_tokens),
        ),
    ]


async def run(tmp_path, output_tokens, *, budget, enabled=True):
    provider = WireProvider([truncated(output_tokens)])
    tools = RecordingTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        tools,
        budget_capped_truncation=enabled,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="cut", budget=budget)
    (request,) = provider.requests
    assert turn.status is TurnStatus.FAILED
    # 截断响应里的工具提案既未持久化为调用，也未执行。
    assert tools.calls == []
    assert not any(isinstance(item.content, ToolCallContent) for item in turn.items)
    return turn, request


async def test_truncation_at_budget_lowered_cap_is_budget_exhaustion(tmp_path):
    turn, request = await run(tmp_path, 0, budget=Budget(max_tokens=50_000))
    assert turn.error.code == "provider_max_output_tokens"
    cap = request.remaining_tokens
    assert cap < Budget().max_output_chars
    turn, _ = await run(tmp_path / "used", cap, budget=Budget(max_tokens=50_000))
    assert turn.error.code == "budget_exceeded"


async def test_truncation_below_the_lowered_cap_keeps_the_provider_failure(tmp_path):
    # 输出未用满预算压低的上限，说明是更紧的Provider上限截断，不能视为预算耗尽。
    _, probe = await run(tmp_path / "probe", 0, budget=Budget(max_tokens=50_000))
    turn, _ = await run(tmp_path, probe.remaining_tokens - 1, budget=Budget(max_tokens=50_000))
    assert turn.error.code == "provider_max_output_tokens"


async def test_truncation_at_the_unlowered_cap_keeps_the_provider_failure(tmp_path):
    budget = Budget(max_tokens=1_000_000, max_output_chars=4_096)
    turn, request = await run(tmp_path, 4_096, budget=budget)
    assert request.remaining_tokens == 4_096
    assert turn.error.code == "provider_max_output_tokens"


async def test_classification_is_off_by_default(tmp_path):
    _, probe = await run(tmp_path / "probe", 0, budget=Budget(max_tokens=50_000))
    turn, _ = await run(
        tmp_path, probe.remaining_tokens, budget=Budget(max_tokens=50_000), enabled=False
    )
    assert turn.error.code == "provider_max_output_tokens"


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_switch_requires_literal_boolean(tmp_path, value):
    with pytest.raises(KernelError):
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            WireProvider([]),
            RecordingTools(),
            budget_capped_truncation=value,
        )
