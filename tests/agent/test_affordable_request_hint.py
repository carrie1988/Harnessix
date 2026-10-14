"""可负担请求提示：由账本实际用量推算，只进入预算说明，准入仍预付最长文本。"""

from __future__ import annotations

import pytest

from harnessix.agent.models import Budget, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.turn_budget_hint import MAX_AFFORDABLE_REQUESTS, affordable_request_hint
from harnessix.models._history import budget_instruction
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_provider_input_budget import WireProvider
from tests.agent.test_rewritten_input_budget import read_step


async def run(tmp_path, provider, *, report, budget=None):
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        RecordingTools(),
        report_affordable_requests=report,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        return await runtime.run_turn(
            thread.thread_id, "任务", request_id="hint", budget=budget or Budget(max_tokens=50_000)
        )


async def test_hint_uses_last_actual_request_size_and_is_off_by_default(tmp_path):
    provider = WireProvider([read_step(1, 9_000), read_step(2, 10_000), answer()])
    turn = await run(tmp_path / "on", provider, report=True)
    assert turn.status is TurnStatus.COMPLETED
    first, second, third = provider.requests
    assert first.affordable_requests is None
    # 第2次请求前：剩余 50000-9050，上一请求 9050 → 4 次（含本次）。
    assert second.affordable_requests == (50_000 - 9_050) // 9_050
    assert third.affordable_requests == (50_000 - 9_050 - 10_050) // 10_050
    assert "requests fit, including this one" in budget_instruction(second)
    default = WireProvider([read_step(1, 9_000), answer()])
    await run(tmp_path / "off", default, report=False)
    assert all(request.affordable_requests is None for request in default.requests)
    assert "requests fit" not in budget_instruction(default.requests[-1])


async def test_admission_prepays_the_longest_hint_text(tmp_path):
    provider = WireProvider([read_step(1, 9_000), answer()])
    await run(tmp_path, provider, report=True)
    sent = provider.requests[-1]
    assert sent.affordable_requests is not None
    candidate = sent.model_copy(
        update={
            "affordable_requests": MAX_AFFORDABLE_REQUESTS,
            "input_tokens_upper_bound": sent.budget.max_tokens,
        }
    )
    prefix = len(provider.requests[0].history)

    def bound(request):
        anchored = 9_000 + provider.adapter.estimate_input_tokens(request, prefix_items=prefix)
        return min(provider.adapter.estimate_input_tokens(request), anchored)

    assert sent.input_tokens_upper_bound == bound(candidate) >= bound(sent)


def test_hint_is_absent_without_complete_usage(tmp_path):
    class _Turn:
        model_attempts = ()
        accounted_attempts = ()

    assert affordable_request_hint(_Turn()) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_runtime_switch_requires_literal_boolean(tmp_path, value):
    from harnessix.agent.errors import KernelError

    with pytest.raises(KernelError):
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            WireProvider([]),
            RecordingTools(),
            report_affordable_requests=value,
        )
