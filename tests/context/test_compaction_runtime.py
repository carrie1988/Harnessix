from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import Budget, TextContent, Usage
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS, AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.attempt_helpers import attempt_start, observed
from tests.agent.helpers import RecordingTools, answer
from tests.artifacts.helpers import exercise
from tests.helpers import RecordingObservability


def accounted_text(text: str, *, step: int = 1) -> list[ProviderEvent]:
    start = attempt_start(step=step)
    return [
        start,
        ResponseStarted(response_id="response-1"),
        TextStarted(content_id="text-1"),
        TextDelta(content_id="text-1", delta=text),
        observed(
            start,
            completeness="complete",
            input_tokens=10,
            output_tokens=3,
        ),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        TextCompleted(content_id="text-1", text=text),
        ResponseCompleted(usage=Usage(input_tokens=10, output_tokens=3)),
    ]


class SequenceProvider:
    def __init__(self, responses: Sequence[Sequence[ProviderEvent]]) -> None:
        self.responses = tuple(tuple(response) for response in responses)
        self.requests: list[ModelRequest] = []
        self.closed_streams = 0

    async def stream(self, request: ModelRequest, cancel: CancelToken):
        index = len(self.requests)
        self.requests.append(request.model_copy(deep=True))
        try:
            if index >= len(self.responses):
                raise KernelError("script_exhausted", "测试Provider响应耗尽")
            for event in self.responses[index]:
                cancel.checkpoint()
                yield event.model_copy(deep=True)
        finally:
            self.closed_streams += 1


def config(**fields) -> CompactionRuntimeConfig:
    values = {
        "trigger_history_tokens": 3000,
        "max_summary_output_tokens": 128,
        **fields,
    }
    return CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=2500,
            summary_reserve_tokens=1024,
            max_summary_input_tokens=100_000,
            retain_recent_groups=1,
            min_savings_tokens=256,
        ),
        **values,
    )


async def run_seeded(
    tmp_path: Path,
    summary_provider,
    *,
    tools=None,
    second_budget: Budget | None = None,
    compaction_config: CompactionRuntimeConfig | None = None,
    observability=None,
):
    long_answer = "解析器调查事实与失败恢复约束。\n" * 600
    normal = SequenceProvider([accounted_text(long_answer), accounted_text("已完成后续修复。")])
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with AgentRuntime(
        store,
        normal,
        tools,
        compaction=compaction_config or config(),
        summary_provider=summary_provider,
        observability=observability,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        seeded = await runtime.run_turn(thread.thread_id, "调查解析器", request_id="seed")
        assert seeded.status == "completed"
        result = await runtime.run_turn(
            thread.thread_id,
            "继续修复并保持public接口",
            request_id="continue",
            budget=second_budget,
        )
    return store, normal, result


async def test_proactive_compaction_uses_accounted_toolless_request_and_active_window(
    tmp_path: Path,
):
    summary = ScriptedProvider([accounted_text("保留public接口；解析器失败恢复仍需验证。")])
    observer = RecordingObservability()
    store, normal, turn = await run_seeded(tmp_path, summary, observability=observer)

    assert turn.status == "completed"
    assert turn.usage == Usage(input_tokens=20, output_tokens=6)
    assert turn.usage_is_complete
    assert len(turn.compactions) == 1
    record = turn.compactions[0]
    assert record.status == "summarized" and record.attempt.status == "completed"
    thread = await store.get_thread(turn.compactions[0].plan.thread_id)
    assert len(thread.compaction_windows) == 1
    assert thread.active_compaction_window_id == thread.compaction_windows[0].window_id

    assert len(summary.requests) == 1 and summary.closed_streams == 1
    summary_request = summary.requests[0]
    assert summary_request.tools == ()
    assert summary_request.instructions == SUMMARY_INSTRUCTIONS
    assert summary_request.remaining_tokens == 128
    assert len(summary_request.history) == 1
    assert isinstance(summary_request.history[0].content, TextContent)
    assert summary_request.history[0].content.kind == "user_message"
    source = json.loads(summary_request.history[0].content.text)
    assert source["trust"] == "untrusted_history" and source["authority"] == "none"
    chat, _ = chat_request(summary_request, OpenAIChatConfig(model="fixture"))
    anthropic, _ = anthropic_request(summary_request, AnthropicConfig(model="fixture"))
    assert "tools" not in chat and "tools" not in anthropic
    assert chat["messages"][0] == {"role": "system", "content": SUMMARY_INSTRUCTIONS}
    assert anthropic["system"] == SUMMARY_INSTRUCTIONS
    assert chat["messages"][-1]["role"] == anthropic["messages"][-1]["role"] == "user"

    assert len(normal.requests) == 2
    projected = "\n".join(
        item.content.text
        for item in normal.requests[-1].history
        if isinstance(item.content, TextContent)
    )
    assert "derived_history" in projected
    assert "解析器调查事实与失败恢复约束" not in projected

    payloads = [
        event.payload.type
        for event in await store.events(thread.thread_id)
        if event.turn_id == turn.turn_id
    ]
    ordered = [
        "compaction_planned",
        "compaction_attempt_started",
        "compaction_usage_observed",
        "compaction_attempt_finished",
        "compaction_summarized",
        "compaction_window_activated",
        "model_history_prepared",
    ]
    positions = [payloads.index(kind) for kind in ordered]
    assert positions == sorted(positions)
    compaction_metrics = [
        metric
        for metric in observer.metrics
        if metric[1] == "harnessix.agent.operations" and metric[3].get("operation") == "compaction"
    ]
    assert len(compaction_metrics) == 1
    assert compaction_metrics[0][3] == {
        "operation": "compaction",
        "outcome": "completed",
    }
    labels = json.dumps([metric[3] for metric in observer.metrics])
    assert "保留public接口" not in labels and str(thread.thread_id) not in labels


async def test_summary_provider_without_first_intent_fails_with_unknown_charge_risk(
    tmp_path: Path,
):
    summary = ScriptedProvider([answer("无账本摘要")])
    _, normal, turn = await run_seeded(tmp_path, summary)
    record = turn.compactions[0]

    assert turn.status == "failed"
    assert turn.error.code == "provider_summary_accounting_required"
    assert record.status == "failed" and record.attempt is None
    assert record.unaccounted_request_possible
    assert len(normal.requests) == 1
    assert summary.closed_streams == 1


async def test_summary_tool_call_is_rejected_without_execution(tmp_path: Path):
    start = attempt_start()
    summary = ScriptedProvider(
        [
            [
                start,
                ResponseStarted(response_id="response-1"),
                ToolCallCompleted(call_id="call-1", tool="test.read", arguments={}),
            ]
        ]
    )
    tools = RecordingTools()
    _, normal, turn = await run_seeded(tmp_path, summary, tools=tools)
    record = turn.compactions[0]

    assert turn.status == "failed"
    assert turn.error.code == "context_compaction_summary_tool_forbidden"
    assert record.status == "failed" and record.attempt.status == "failed"
    assert not record.unaccounted_request_possible
    assert not tools.calls and len(normal.requests) == 1
    assert summary.closed_streams == 1


async def test_summary_retry_is_stopped_before_second_request(tmp_path: Path):
    first, second = attempt_start(), attempt_start(index=2)

    class RetryingProvider:
        def __init__(self) -> None:
            self.requests = []
            self.http_requests = 0
            self.closed = 0

        async def stream(self, request, cancel):
            self.requests.append(request)
            try:
                yield first
                self.http_requests += 1
                yield observed(
                    first,
                    completeness="complete",
                    input_tokens=7,
                    output_tokens=2,
                )
                yield ModelAttemptFinished(
                    attempt_id=first.attempt_id,
                    outcome="failed",
                    error=AgentFailure(
                        code="provider_transport", message="传输失败", retryable=True
                    ),
                )
                yield second
                self.http_requests += 1
                raise AssertionError("第二个摘要HTTP请求不得发出")
            finally:
                self.closed += 1

    summary = RetryingProvider()
    _, normal, turn = await run_seeded(tmp_path, summary)
    record = turn.compactions[0]

    assert turn.status == "failed" and turn.error.code == "invalid_provider_output"
    assert summary.http_requests == 1 and summary.closed == 1
    assert len(normal.requests) == 1
    assert record.status == "failed" and record.attempt.attempt_id == first.attempt_id
    assert record.attempt.status == "failed"
    assert turn.usage == Usage(input_tokens=7, output_tokens=2)


async def test_summary_response_usage_must_match_settled_attempt(tmp_path: Path):
    events = accounted_text("摘要正文")
    events[-1] = ResponseCompleted(usage=Usage(input_tokens=9, output_tokens=3))
    summary = ScriptedProvider([events])
    _, normal, turn = await run_seeded(tmp_path, summary)

    assert turn.status == "failed" and turn.error.code == "invalid_provider_output"
    assert len(normal.requests) == 1
    assert turn.compactions[0].status == "failed"
    assert turn.compactions[0].attempt.status == "completed"
    assert turn.usage == Usage(input_tokens=10, output_tokens=3)


async def test_cancel_during_summary_closes_stream_and_ledger(tmp_path: Path):
    entered = asyncio.Event()
    release = asyncio.Event()
    start = attempt_start()

    class BlockingProvider:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def stream(self, request, cancel):
            try:
                yield start
                yield observed(
                    start,
                    completeness="partial",
                    input_tokens=8,
                    output_tokens=1,
                )
                entered.set()
                await release.wait()
            finally:
                self.closed.set()

    summary = BlockingProvider()
    long_answer = "长历史。\n" * 1000
    normal = SequenceProvider([accounted_text(long_answer), accounted_text("不应调用")])
    store = SQLiteSessionStore(tmp_path / "cancel.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(),
        summary_provider=summary,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        task = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "继续任务", request_id="cancel")
        )
        await asyncio.wait_for(entered.wait(), 2)
        snapshot = await store.get_thread(thread.thread_id)
        assert snapshot.active_turn_id is not None
        await runtime.cancel(thread.thread_id, snapshot.active_turn_id)
        turn = await asyncio.wait_for(task, 2)
        await asyncio.wait_for(summary.closed.wait(), 2)

    assert turn.status == "cancelled"
    assert turn.compactions[0].status == "cancelled"
    assert turn.compactions[0].attempt.status == "cancelled"
    assert turn.usage == Usage(input_tokens=8, output_tokens=1)
    assert len(normal.requests) == 1


def test_runtime_compaction_configuration_is_explicit_and_bounded(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "config.sqlite")
    provider = ScriptedProvider([answer()])
    with pytest.raises(KernelError, match="同时提供"):
        AgentRuntime(store, provider, compaction=config())
    with pytest.raises(KernelError, match="同时提供"):
        AgentRuntime(store, provider, summary_provider=provider)
    with pytest.raises(ValueError, match="高于"):
        CompactionRuntimeConfig(
            policy=config().policy,
            trigger_history_tokens=config().policy.target_history_tokens,
            max_summary_output_tokens=128,
        )


def context_overflow_events(*, step: int, with_text: bool = False) -> list[ProviderEvent]:
    start = attempt_start(step=step)
    events: list[ProviderEvent] = [start]
    if with_text:
        events.extend(
            [
                ResponseStarted(response_id="overflow-response"),
                TextStarted(content_id="partial"),
                TextDelta(content_id="partial", delta="部分输出"),
                TextCompleted(content_id="partial", text="部分输出"),
            ]
        )
    events.extend(
        [
            observed(
                start,
                completeness="complete",
                input_tokens=5,
                output_tokens=0,
            ).model_copy(update={"response_id": "overflow-response"} if with_text else {}),
            ModelAttemptFinished(
                attempt_id=start.attempt_id,
                outcome="failed",
                error=AgentFailure(
                    code="provider_context_overflow", message="上下文超过Provider窗口"
                ),
            ),
            ResponseFailed(code="context_overflow"),
        ]
    )
    return events


async def test_accounted_context_overflow_forces_one_compaction_then_next_step(
    tmp_path: Path,
):
    long_answer = "上下文历史事实。\n" * 600
    normal = SequenceProvider(
        [
            accounted_text(long_answer),
            context_overflow_events(step=1),
            accounted_text("压缩后完成。", step=2),
        ]
    )
    summary = ScriptedProvider([[], accounted_text("保留任务约束与未完成事项。", step=2)])
    store = SQLiteSessionStore(tmp_path / "reactive.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(trigger_history_tokens=50_000),
        summary_provider=summary,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        turn = await runtime.run_turn(thread.thread_id, "继续任务", request_id="reactive")

    assert turn.status == "completed" and turn.model_steps == 2
    assert [attempt.status for attempt in turn.model_attempts] == ["failed", "completed"]
    assert turn.model_attempts[0].error.code == "provider_context_overflow"
    assert len(turn.compactions) == 1 and turn.compactions[0].plan.model_step == 2
    assert turn.compactions[0].status == "summarized"
    assert len(summary.requests) == 1 and summary.requests[0].step == 2
    assert len(normal.requests) == 3 and normal.requests[-1].step == 2
    assert turn.usage == Usage(input_tokens=25, output_tokens=6)
    assert turn.usage_is_complete


async def test_second_context_overflow_stops_at_no_progress_without_another_paid_summary(
    tmp_path: Path,
):
    normal = SequenceProvider(
        [
            accounted_text("长历史。\n" * 1200),
            context_overflow_events(step=1),
            context_overflow_events(step=2),
        ]
    )
    summary = ScriptedProvider([[], accounted_text("保留全部关键约束。", step=2)])
    store = SQLiteSessionStore(tmp_path / "repeat-overflow.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(trigger_history_tokens=50_000),
        summary_provider=summary,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        turn = await runtime.run_turn(thread.thread_id, "继续任务", request_id="overflow")

    assert turn.status == "failed"
    assert turn.error.code == "context_compaction_no_progress"
    assert len(summary.requests) == 1
    assert len(normal.requests) == 3
    assert len(turn.compactions) == 1 and turn.compactions[0].status == "summarized"


async def test_context_overflow_after_semantic_output_cannot_rewrite_history(tmp_path: Path):
    normal = SequenceProvider(
        [
            accounted_text("长历史。\n" * 1200),
            context_overflow_events(step=1, with_text=True),
        ]
    )
    summary = ScriptedProvider([accounted_text("不应请求")])
    store = SQLiteSessionStore(tmp_path / "exposed-overflow.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(trigger_history_tokens=50_000),
        summary_provider=summary,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        turn = await runtime.run_turn(thread.thread_id, "继续任务", request_id="overflow")

    assert turn.status == "failed" and turn.error.code == "invalid_event"
    assert not summary.requests
    assert any(
        isinstance(item.content, TextContent) and item.content.text == "部分输出"
        for item in turn.items
    )


async def test_covered_artifact_is_verified_before_summary_request(tmp_path: Path):
    store, artifacts, _, thread, _ = await exercise(tmp_path, count=300)
    with sqlite3.connect(store.path) as database:
        database.execute("UPDATE agent_artifacts SET body = zeroblob(size_bytes)")
    summary = ScriptedProvider([accounted_text("不应请求")])
    normal = FakeProvider()
    compact = CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=1024,
            summary_reserve_tokens=256,
            max_summary_input_tokens=100_000,
            retain_recent_groups=1,
            min_savings_tokens=128,
        ),
        trigger_history_tokens=1025,
        max_summary_output_tokens=128,
    )
    root = tmp_path / "repo"
    assert isinstance(artifacts, SQLiteArtifactStore)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            normal,
            scoped_tools=tools,
            artifacts=artifacts,
            tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=2048),
            compaction=compact,
            summary_provider=summary,
        ) as runtime:
            failed = await runtime.run_turn(
                thread.thread_id, "继续分析", request_id="artifact-corrupt"
            )

    assert failed.status == "failed" and failed.error.code == "artifact_corrupt"
    assert not summary.requests and not normal.requests
    assert not failed.compactions


async def test_multiple_turns_publish_linear_windows_without_restoring_old_prefix(
    tmp_path: Path,
):
    first_history = "第一轮长历史事实。\n" * 700
    second_history = "第二轮长历史事实。\n" * 700
    normal = SequenceProvider(
        [
            accounted_text(first_history),
            accounted_text(second_history),
            accounted_text("第三轮完成。"),
        ]
    )
    summaries = SequenceProvider(
        [
            accounted_text("第一窗口保留目标与约束。"),
            accounted_text("第二窗口保留目标、约束和当前进度。"),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "repeated.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(),
        summary_provider=summaries,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "第一轮", request_id="one")
        second = await runtime.run_turn(thread.thread_id, "第二轮", request_id="two")
        third = await runtime.run_turn(thread.thread_id, "第三轮", request_id="three")

    assert second.status == third.status == "completed"
    assert len(summaries.requests) == 2
    snapshot = await store.get_thread(thread.thread_id)
    assert len(snapshot.compaction_windows) == 2
    assert snapshot.compaction_windows[0].previous_window_id is None
    assert (
        snapshot.compaction_windows[1].previous_window_id
        == snapshot.compaction_windows[0].window_id
    )
    assert snapshot.active_compaction_window_id == snapshot.compaction_windows[-1].window_id
    final_text = "\n".join(
        item.content.text
        for item in normal.requests[-1].history
        if isinstance(item.content, TextContent)
    )
    assert "第二窗口保留目标" in final_text
    assert "第一轮长历史事实" not in final_text
    assert "第二轮长历史事实" not in final_text


async def test_task_cancel_after_summary_commit_still_publishes_adjacent_window(
    tmp_path: Path,
):
    normal = SequenceProvider([accounted_text("长历史。\n" * 1200), accounted_text("不应调用")])
    summary = ScriptedProvider([accounted_text("保留目标和当前进度。")])

    def cancel_after_candidate(name: str) -> None:
        if name == "runtime.after_compaction_summarized":
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    store = SQLiteSessionStore(tmp_path / "candidate-cancel.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(),
        summary_provider=summary,
        fault=cancel_after_candidate,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        with pytest.raises(asyncio.CancelledError):
            await runtime.run_turn(thread.thread_id, "继续任务", request_id="cancel")

    snapshot = await store.get_thread(thread.thread_id)
    turn = snapshot.turns[-1]
    assert turn.status == "cancelled"
    assert turn.compactions[0].status == "summarized"
    assert len(snapshot.compaction_windows) == 1
    events = await store.events(thread.thread_id)
    index = next(
        index for index, event in enumerate(events) if event.payload.type == "compaction_summarized"
    )
    assert events[index + 1].payload.type == "compaction_window_activated"
    assert len(normal.requests) == 1


async def test_summary_wait_obeys_original_turn_deadline(tmp_path: Path):
    entered = asyncio.Event()

    class BlockingSummary:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def stream(self, request, cancel):
            start = attempt_start()
            try:
                yield start
                entered.set()
                await asyncio.Event().wait()
            finally:
                self.closed.set()

    normal = SequenceProvider([accounted_text("长历史。\n" * 1200), accounted_text("不应调用")])
    summary = BlockingSummary()
    store = SQLiteSessionStore(tmp_path / "summary-timeout.sqlite")
    async with AgentRuntime(
        store,
        normal,
        compaction=config(),
        summary_provider=summary,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "建立历史", request_id="seed")
        turn = await runtime.run_turn(
            thread.thread_id,
            "继续任务",
            request_id="timeout",
            budget=Budget(timeout_seconds=1),
        )
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(summary.closed.wait(), 1)

    assert turn.status == "failed" and turn.error.code == "time_budget_exceeded"
    assert turn.compactions[0].status == "failed"
    assert turn.compactions[0].attempt.status == "failed"
    assert len(normal.requests) == 1
