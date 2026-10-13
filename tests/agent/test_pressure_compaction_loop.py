"""真实 Runtime 内提前压缩、结算及重放；不使用云模型或评测答案。"""

from pathlib import Path

import pytest

from harnessix.agent.models import Budget, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.models.contracts import ResponseCompleted, ToolCallCompleted
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.context.test_compaction_runtime import SequenceProvider, accounted_text


@pytest.mark.parametrize("enabled", [False, True])
async def test_pressure_compaction_requires_explicit_opt_in_and_replays(tmp_path: Path, enabled):
    responses = []
    for step in range(1, 4):
        events = accounted_text("a" * 2500, step=step)
        events.insert(-1, ToolCallCompleted(call_id=f"read-{step}", tool="test.read"))
        events[-1] = ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
        )
        responses.append(events)
    responses.append(accounted_text("完成复核", step=4))
    main = SequenceProvider(responses)
    summary = SequenceProvider([accounted_text("已调查三个窗口；保留未完成事项。", step=4)])
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    config = CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=65536,
            summary_reserve_tokens=12288,
            max_summary_input_tokens=262144,
            retain_recent_groups=2,
        ),
        trigger_history_tokens=131072,
        max_summary_output_tokens=2048,
    )
    async with AgentRuntime(
        store,
        main,
        RecordingTools(),
        compaction=config,
        summary_provider=summary,
        **({"budget_pressure_compaction": True} if enabled else {}),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "调查、修复并验证",
            request_id="pressure",
            budget=Budget(max_tokens=20000, max_steps=16),
        )
    assert turn.status == "completed"
    assert len(summary.requests) == int(enabled) and len(main.requests) == 4
    assert len(turn.accounted_attempts) == 4 + int(enabled) <= turn.budget.max_steps
    assert turn.usage == Usage(input_tokens=40 + 10 * enabled, output_tokens=12 + 3 * enabled)
    if enabled:
        record = turn.compactions[0]
        assert record.status == "summarized"
        assert record.plan.source_history_tokens < config.trigger_history_tokens
        assert record.plan.policy.retain_recent_groups >= 2
        assert record.plan.policy.target_history_tokens < config.policy.target_history_tokens
    else:
        assert not turn.compactions
    restored = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == restored
    assert (restored.active_compaction_window_id is not None) == enabled
