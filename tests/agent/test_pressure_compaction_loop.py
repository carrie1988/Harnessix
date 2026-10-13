"""真实 Runtime 内提前压缩、结算及重放；不使用云模型或评测答案。"""

import json
import re
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from harnessix.agent.models import Budget, Usage
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted, ToolCallCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.context.test_compaction_runtime import SequenceProvider, accounted_text
from tests.models.wire import WireStream, chunk, frame, response


class BudgetSummaryProvider(SequenceProvider):
    async def stream(self, request, cancel):
        # 摘要使用规划时的原请求步骤，不在夹具中猜测何时满足摊销准入。
        self.responses = (
            *self.responses,
            tuple(accounted_text("已调查旧窗口；保留未完成事项。", step=request.step)),
        )
        async with aclosing(super().stream(request, cancel)) as stream:
            async for event in stream:
                yield event


def investigation_responses(groups):
    responses = []
    for step in range(1, groups + 1):
        events = accounted_text("a" * 2500, step=step)
        events.insert(-1, ToolCallCompleted(call_id=f"read-{step}", tool="test.read"))
        events[-1] = ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
        )
        responses.append(events)
    responses.append(accounted_text("完成复核", step=groups + 1))
    return responses


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("groups,tokens,compactions", [(3, 20000, 0), (9, 50000, 2)])
async def test_pressure_compaction_requires_explicit_opt_in_and_replays(
    tmp_path: Path, enabled, groups, tokens, compactions
):
    main = SequenceProvider(investigation_responses(groups))
    summary = BudgetSummaryProvider([])
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
            budget=Budget(max_tokens=tokens, max_steps=16),
        )
    assert turn.status == "completed", turn.error
    expected_compactions = compactions if enabled else 0
    count = groups + 1 + expected_compactions
    assert len(summary.requests) == expected_compactions and len(main.requests) == groups + 1
    assert len(turn.accounted_attempts) == count <= turn.budget.max_steps
    assert turn.usage == Usage(input_tokens=10 * count, output_tokens=3 * count)
    if expected_compactions:
        for record, request in zip(turn.compactions, summary.requests, strict=True):
            assert record.status == "summarized"
            assert record.plan.source_history_tokens < config.trigger_history_tokens
            assert record.plan.policy.retain_recent_groups >= 2
            assert record.plan.policy.target_history_tokens < config.policy.target_history_tokens
            assert f"at most {request.remaining_tokens} tokens" in request.instructions
            assert request.remaining_tokens > 178
    else:
        assert not turn.compactions
    restored = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == restored
    assert (restored.active_compaction_window_id is not None) == bool(expected_compactions)


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_pressure_summary_capacity_reaches_sdk_and_terminal_facts_replay(
    tmp_path: Path, monkeypatch, finish
):
    monkeypatch.setenv("HARNESSIX_SUMMARY_FIXTURE_KEY", "offline-key")
    requests, wires = [], []
    provider_config = OpenAIChatConfig(
        model="test-model",
        api_key_env="HARNESSIX_SUMMARY_FIXTURE_KEY",
        max_attempts=1,
        max_output_tokens=2048,
    )

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        instructions = body["messages"][0]["content"]
        capacity = re.search(r"at most (\d+) tokens", instructions)
        assert capacity is not None
        assert body[provider_config.output_token_parameter] == int(capacity[1]) > 178
        assert "tools" not in body and "a" * 2500 not in instructions
        usage = chunk(usage=True)
        usage["usage"] = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        wire = WireStream(
            [
                frame(chunk({"role": "assistant", "content": "已调查旧窗口；尚需完成后续工作。"})),
                frame(chunk(finish=finish)),
                frame(usage),
                b"data: [DONE]\n\n",
            ]
        )
        wires.append(wire)
        return response(wire)

    main = SequenceProvider(investigation_responses(9))
    store = SQLiteSessionStore(tmp_path / "sdk.sqlite")
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
    async with OpenAIChatProvider(
        provider_config, transport=httpx.MockTransport(handle)
    ) as summary:
        async with AgentRuntime(
            store,
            main,
            RecordingTools(),
            compaction=config,
            summary_provider=summary,
            budget_pressure_compaction=True,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id,
                "调查、修复并验证",
                request_id="sdk-pressure",
                budget=Budget(max_tokens=50000, max_steps=16),
            )
    assert all(wire.closed for wire in wires)
    assert len(turn.accounted_attempts) == len(main.requests) + len(requests) <= 16
    assert turn.usage == Usage(
        input_tokens=10 * len(turn.accounted_attempts),
        output_tokens=3 * len(turn.accounted_attempts),
    )
    if finish == "stop":
        assert turn.status == "completed", turn.error
        assert len(requests) == len(turn.compactions) == 2
        assert all(record.status == "summarized" for record in turn.compactions)
        assert len(main.requests) == 10
    else:
        assert turn.status == "failed" and turn.error.code == "invalid_provider_output"
        assert "达到Token上限" in turn.error.message
        assert len(requests) == len(turn.compactions) == 1
        assert turn.compactions[0].status == "failed"
        assert turn.compactions[0].attempt.usage.completeness == "complete"
        assert len(main.requests) < 10
    restored = await store.get_thread(thread.thread_id)
    assert restored == replay(await store.events(thread.thread_id))
    assert (restored.active_compaction_window_id is not None) == (finish == "stop")


@pytest.mark.parametrize("drift", ["tools", "choice", "forced_tool", "schema", "cancel"])
async def test_catalog_drift_after_summary_cannot_open_next_paid_main_request(
    tmp_path: Path, drift
):
    class AlternateReadTools(RecordingTools):
        def definitions(self):
            primary = super().definitions()[0]
            return primary, primary.model_copy(update={"name": "test.other"})

    class DriftingView:
        def select(self, turn, definitions):
            if drift == "cancel" and turn.compactions:
                pytest.fail("取消检查应早于摘要后的工具目录重新选择")
            tools = tuple(d for d in definitions if d.name == "test.read")
            if not turn.compactions or drift == "schema":
                return ModelToolSelection(tools)
            if drift == "tools":
                return ModelToolSelection(tuple(d for d in definitions if d.name == "test.other"))
            return ModelToolSelection(
                tools, "required", "test.read" if drift == "forced_tool" else None
            )

    tools = AlternateReadTools()
    main = SequenceProvider(investigation_responses(9))

    class ChangingSummary(BudgetSummaryProvider):
        async def stream(self, request, cancel):
            if drift == "schema":
                # 改动嵌套Schema，证明预付快照不能与可变注册项共享引用。
                runtime._definitions["test.read"].input_schema["properties"] = {
                    "changed": {"type": "string"}
                }
            async with aclosing(super().stream(request, cancel)) as stream:
                async for event in stream:
                    yield event

    summary = ChangingSummary([])

    def cancel_after_history(stage):
        if (
            drift == "cancel"
            and stage == "runtime.after_model_history_prepared"
            and summary.requests
        ):
            for _, token, _ in runtime._active.values():
                token.cancel()

    store = SQLiteSessionStore(tmp_path / "drift.sqlite")
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
        tools,
        compaction=config,
        summary_provider=summary,
        budget_pressure_compaction=True,
        model_tool_view=DriftingView(),
        fault=cancel_after_history,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "调查、修复并验证",
            request_id="catalog-drift",
            budget=Budget(max_tokens=50000, max_steps=16),
        )
    if drift == "cancel":
        assert turn.status == "cancelled" and turn.error.code == "cancelled"
    else:
        assert turn.status == "failed" and turn.error.code == "context_compaction_source_changed"
        assert "目录或选择约束" in turn.error.message
    assert len(summary.requests) == 1 and len(turn.compactions) == 1
    record = turn.compactions[0]
    assert record.status == "summarized"
    assert record.plan.model_step == len(main.requests) + 1
    assert len(tools.calls) == len(main.requests)
    assert all(call.tool == "test.read" for call in tools.calls)
    assert len(turn.model_attempts) == len(main.requests)
    restored = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == restored
    before = await store.events(thread.thread_id)
    async with AgentRuntime(store, main, tools):
        assert await store.get_thread(thread.thread_id) == restored
    assert await store.events(thread.thread_id) == before
    assert len(summary.requests) == 1
