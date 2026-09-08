from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.ids import new_id
from harnessix.agent.models import TextContent, TurnStatus, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import RETRY_INSTRUCTIONS, AgentRuntime
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.tool_result_contracts import ModelHistoryInspectionV2
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.costs import COST_REPORT_ADAPTER, build_cost_report
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools


class SequenceProvider:
    def __init__(self, responses: Sequence[Sequence[ProviderEvent]]) -> None:
        self.responses = tuple(tuple(response) for response in responses)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancel: CancelToken):
        index = len(self.requests)
        self.requests.append(request.model_copy(deep=True))
        for event in self.responses[index]:
            cancel.checkpoint()
            yield event.model_copy(deep=True)


def _accounted(
    *,
    step: int,
    provider: str,
    text: str | None = None,
    tool: bool = False,
) -> list[ProviderEvent]:
    attempt = ModelAttemptStarted(
        attempt_id=new_id(),
        step=step,
        index=1,
        provider=provider,
        requested_model=f"{provider}-model",
    )
    events: list[ProviderEvent] = [
        attempt,
        ResponseStarted(response_id=f"{provider}-{step}"),
    ]
    if tool:
        events.append(ToolCallCompleted(call_id="native-call", tool="test.read", arguments={}))
    else:
        assert text is not None
        events.extend(
            [
                TextStarted(content_id="answer"),
                TextDelta(content_id="answer", delta=text),
            ]
        )
    events.extend(
        [
            ModelUsageObserved(
                attempt_id=attempt.attempt_id,
                actual_model=f"{provider}-model-v1",
                response_id=f"{provider}-{step}",
                usage=UsageObservation(
                    completeness="complete",
                    input_tokens=10,
                    output_tokens=3,
                ),
            ),
            ModelAttemptFinished(attempt_id=attempt.attempt_id, outcome="completed"),
        ]
    )
    if not tool:
        assert text is not None
        events.append(TextCompleted(content_id="answer", text=text))
    events.append(
        ResponseCompleted(
            finish_reason="tool_calls" if tool else "completed",
            usage=Usage(input_tokens=10, output_tokens=3),
        )
    )
    return events


def _compaction_config() -> CompactionRuntimeConfig:
    return CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=2500,
            summary_reserve_tokens=1024,
            max_summary_input_tokens=100_000,
            retain_recent_groups=1,
            min_savings_tokens=256,
        ),
        trigger_history_tokens=3000,
        max_summary_output_tokens=128,
    )


class SimulatedProcessExit(BaseException):
    pass


async def test_long_session_compaction_recovery_retry_switch_fork_archive(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "long-session.sqlite")
    tools = RecordingTools()
    long_answer = "保留接口约束、文件revision、测试结论和未完成事项。\n" * 700
    generation = SequenceProvider(
        [
            _accounted(step=1, provider="openai_chat", tool=True),
            _accounted(step=2, provider="openai_chat", text=long_answer),
            _accounted(step=1, provider="openai_chat", text="压缩后继续完成。"),
        ]
    )
    summary = SequenceProvider(
        [_accounted(step=1, provider="summary", text="保留接口约束与未完成验证。")]
    )
    async with AgentRuntime(
        store,
        generation,
        tools,
        compaction=_compaction_config(),
        summary_provider=summary,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        seeded = await runtime.run_turn(thread.thread_id, "读取并调查", request_id="seed")
        compacted = await runtime.run_turn(thread.thread_id, "继续修复", request_id="compact")

    assert seeded.status is TurnStatus.COMPLETED
    assert compacted.status is TurnStatus.COMPLETED
    assert len(tools.calls) == 1
    after_compaction = await store.get_thread(thread.thread_id)
    assert len(after_compaction.compaction_windows) == 1
    original_events = [event.model_dump_json() for event in await store.events(thread.thread_id)]

    def interrupt_after_acceptance(point: str) -> None:
        if point == "runtime.after_turn_started":
            raise SimulatedProcessExit

    try:
        async with AgentRuntime(
            store,
            SequenceProvider([_accounted(step=1, provider="unused", text="不得请求")]),
            tools,
            fault=interrupt_after_acceptance,
        ) as runtime:
            await runtime.run_turn(thread.thread_id, "需要恢复的工作", request_id="interrupted")
    except SimulatedProcessExit:
        pass
    else:
        raise AssertionError("中断故障点未触发")

    retry_provider = SequenceProvider(
        [_accounted(step=1, provider="anthropic", text="恢复并核对完成。")]
    )
    async with AgentRuntime(store, retry_provider, tools) as runtime:
        recovered = await store.get_thread(thread.thread_id)
        interrupted = recovered.turns[-1]
        assert interrupted.status is TurnStatus.INTERRUPTED
        retried = await runtime.retry_turn(
            thread.thread_id,
            interrupted.turn_id,
            request_id="retry",
        )
        forked = await runtime.fork_thread(thread.thread_id, request_id="long-session-fork")
        archived = await runtime.archive_thread(thread.thread_id, reason="0.6长会话验收")

    assert retried.status is TurnStatus.COMPLETED
    assert retried.retry_of_turn_id == interrupted.turn_id
    assert archived.archive is not None
    assert len(tools.calls) == 1
    assert len(retry_provider.requests) == 1
    inspection = retried.model_history_inspections[-1]
    assert isinstance(inspection, ModelHistoryInspectionV2)
    assert inspection.raw_history_items > inspection.history_items
    visible_text = "\n".join(
        item.content.text
        for item in retry_provider.requests[0].history
        if isinstance(item.content, TextContent)
    )
    assert RETRY_INSTRUCTIONS in visible_text
    assert "保留接口约束与未完成验证" in visible_text
    assert long_answer not in visible_text

    persisted_events = await store.events(thread.thread_id)
    assert [event.model_dump_json() for event in persisted_events[: len(original_events)]] == (
        original_events
    )
    persisted = await store.get_thread(thread.thread_id)
    assert replay(persisted_events) == persisted
    assert await store.rebuild(thread.thread_id) == persisted
    assert await store.rebuild(forked.thread_id) == forked
    assert forked.fork_snapshot is not None and forked.fork_snapshot.authority == "none"

    assert [attempt.provider for attempt in seeded.model_attempts] == [
        "openai_chat",
        "openai_chat",
    ]
    assert [attempt.provider for attempt in retried.model_attempts] == ["anthropic"]
    for turn in persisted.turns:
        expected_usage = Usage(
            input_tokens=sum(
                attempt.usage.input_tokens or 0 for attempt in turn.accounted_attempts
            ),
            output_tokens=sum(
                attempt.usage.output_tokens or 0 for attempt in turn.accounted_attempts
            ),
        )
        assert turn.usage == expected_usage
        report = build_cost_report(turn)
        assert COST_REPORT_ADAPTER.validate_json(report.model_dump_json()) == report
