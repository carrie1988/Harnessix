"""原生计划跨真实Runtime压缩及连续窗口回放；没有云模型或分数。"""

import pytest

from harnessix.agent.models import Budget, PlanContent, ToolCallContent, TurnStatus, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_planning_tool import plan_call, proposal_step
from tests.agent.test_pressure_compaction_loop import BudgetSummaryProvider
from tests.context.test_compaction_runtime import context_overflow_events


@pytest.mark.parametrize("mode", ["normal", "reactive", "pressure"])
async def test_native_plan_pins_remain_in_all_real_compaction_windows(tmp_path, mode):
    pressure = mode == "pressure"
    groups = []
    for index in range(8):
        groups.append(
            [
                ResponseStarted(response_id="response"),
                *answer("a" * 2500)[1:-1],
                ToolCallCompleted(call_id=f"read-{index}", tool="test.read"),
                ResponseCompleted(
                    finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
                ),
            ]
        )
    responses = [proposal_step(plan_call()), *groups, answer()]
    if mode == "reactive":
        responses = [
            responses[0],
            *groups[:4],
            context_overflow_events(step=6),
            *groups[4:],
            context_overflow_events(step=11),
            answer(),
        ]
    main = ScriptedProvider(responses)
    summary = BudgetSummaryProvider([])
    store = SQLiteSessionStore(tmp_path / "session.db")
    config = CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=65536 if pressure else 8000,
            summary_reserve_tokens=12288 if pressure else 1024,
            max_summary_input_tokens=262144,
            retain_recent_groups=2 if pressure else 1,
        ),
        trigger_history_tokens=10000 if mode == "normal" else 131072,
        max_summary_output_tokens=2048 if pressure else 128,
    )
    async with AgentRuntime(
        store,
        main,
        RecordingTools(),
        enable_planning=True,
        compaction=config,
        summary_provider=summary,
        budget_pressure_compaction=pressure,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "规划调查和验证",
            request_id="native-plan-window",
            budget=Budget(max_tokens=50_000, max_steps=16, timeout_seconds=900),
        )
    assert turn.status == TurnStatus.COMPLETED, turn.error
    call = next(
        item
        for item in turn.items
        if isinstance(item.content, ToolCallContent) and item.content.tool == "update_plan"
    )
    assert len(turn.compactions) >= 2
    assert len(main.requests) == len(responses)
    assert turn.model_steps + len(turn.compactions) <= 16
    for record in turn.compactions:
        assert record.status == "summarized"
        assert record.plan.anchors[0].item_id == call.item_id
        assert call.item_id in record.plan.retained_item_ids
        assert call.item_id in record.plan.pinned_item_ids
    restored = await store.get_thread(thread.thread_id)
    assert len(restored.compaction_windows) == len(turn.compactions)
    assert all(call.item_id in window.history_item_ids for window in restored.compaction_windows)
    assert replay(await store.events(thread.thread_id)) == restored
    assert all(
        not isinstance(item.content, PlanContent)
        for request in main.requests
        for item in request.history
    )
