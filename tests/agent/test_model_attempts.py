from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harnessix.agent import runtime as runtime_module
from harnessix.agent.errors import AgentFailure
from harnessix.agent.ids import new_id
from harnessix.agent.models import Budget, EventDraft, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import accounted_answer, attempt_start, observed, prepare_attempt
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.context.test_compaction_runtime import config as compaction_config
from tests.deadlines import capture_deadlines


def failed(start):
    return ModelAttemptFinished(
        attempt_id=start.attempt_id,
        outcome="failed",
        error=AgentFailure(code="provider_transport", message="传输失败", retryable=True),
    )


async def execute(tmp_path, events, *, budget=None):
    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = RecordingTools()
    provider = ScriptedProvider([events])
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "核对尝试", request_id="r", budget=budget)
    snapshot = await store.get_thread(thread.thread_id)
    assert snapshot == replay(await store.events(thread.thread_id))
    assert all(a.status != "running" for a in turn.model_attempts)
    assert provider.closed_streams == 1
    return turn, tools


async def test_cumulative_observations_and_response_do_not_double_count(tmp_path: Path) -> None:
    start = attempt_start()
    partial = observed(
        start, completeness="partial", input_tokens=10, output_tokens=1, cache_read_input_tokens=4
    )
    events = accounted_answer(start=start)
    events[1:1] = [partial, partial.model_copy()]
    turn, _ = await execute(tmp_path, events)
    assert turn.status == "completed"
    assert turn.usage == Usage(input_tokens=10, output_tokens=3)
    assert turn.usage_is_complete
    assert turn.usage_step == 1
    receipt = turn.model_attempts[0]
    assert receipt.usage.reasoning_output_tokens == 1
    assert receipt.usage.cache_creation_input_tokens is None
    assert receipt.actual_model == "fixture-model-v1"


@pytest.mark.parametrize("completeness", ["unknown", "partial", "complete"])
async def test_retry_keeps_every_attempt_and_known_usage(tmp_path: Path, completeness) -> None:
    first, second = attempt_start(), attempt_start(index=2)
    counts = {} if completeness == "unknown" else {"input_tokens": 7, "output_tokens": 2}
    events = [
        first,
        observed(first, completeness=completeness, **counts),
        failed(first),
        *accounted_answer(start=second),
    ]
    turn, _ = await execute(tmp_path, events)
    if completeness == "complete":
        assert turn.status == "completed"
        assert [a.status for a in turn.model_attempts] == ["failed", "completed"]
        assert turn.usage == Usage(input_tokens=17, output_tokens=5)
        assert turn.usage_is_complete
    else:
        # 失败尝试的用量未知，重试被计费安全规则拒绝；原因如实归于服务方故障（ADR 0123）。
        assert turn.status == "failed" and turn.error.code == "provider_transport"
        assert turn.error.category == "provider"
        assert len(turn.model_attempts) == 1
        assert not turn.usage_is_complete


async def test_known_retry_budget_is_checked_before_resuming_provider(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    sent = []
    first, second = attempt_start(), attempt_start(index=2)

    class Provider:
        async def stream(self, request, cancel):
            yield first
            assert (await store.get_thread(request.thread_id)).turns[-1].model_attempts[
                0
            ].attempt_id == first.attempt_id
            sent.append(1)
            yield observed(first, completeness="complete", input_tokens=49_000, output_tokens=0)
            yield failed(first)
            yield second
            sent.append(2)
            raise AssertionError("超预算请求不能发送")

    async with AgentRuntime(store, Provider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "预算",
            request_id="r",
            budget=Budget(max_tokens=50_000, max_output_chars=1_024),
        )
    assert sent == [1]
    assert len(turn.model_attempts) == 1
    assert turn.error.code == "budget_exceeded"


@pytest.mark.parametrize("compaction", [False, True])
async def test_refused_retry_after_service_failure_keeps_provider_code(
    tmp_path: Path, compaction: bool
) -> None:
    """矩阵05场景：503后重试被计费安全规则拒绝；开启上下文压缩时由请求额度检查拦截。"""
    first, second = attempt_start(), attempt_start(index=2)
    failure = ModelAttemptFinished(
        attempt_id=first.attempt_id,
        outcome="failed",
        error=AgentFailure(code="provider_provider_internal", message="503", retryable=True),
    )
    store = SQLiteSessionStore(tmp_path / "s.db")
    provider = ScriptedProvider([[first, failure, *accounted_answer(start=second)]])
    options = (
        {"compaction": compaction_config(), "summary_provider": ScriptedProvider([])}
        if compaction
        else {}
    )
    async with AgentRuntime(store, provider, RecordingTools(), **options) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "服务失败", request_id="r")
    assert [a.status for a in turn.model_attempts] == ["failed"]
    assert turn.status == "failed" and turn.error.code == "provider_provider_internal"
    assert turn.error.category == "provider"


def _tool_answer(start):
    return [
        start,
        ResponseStarted(response_id=f"response-{start.index}"),
        ToolCallCompleted(call_id="call-0", tool="test.read", arguments={}),
        observed(start, completeness="complete", input_tokens=10, output_tokens=3),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
        ),
    ]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("provider_transport", "provider_transport"),
        ("provider_provider_internal", "provider_provider_internal"),
        ("provider_rate_limit", "provider_rate_limit"),
        # 非服务方故障（如输出无效）仍按预算规则停止，不借用服务方归因。
        ("provider_invalid_provider_output", "budget_exceeded"),
    ],
)
async def test_recovered_failure_with_unknown_usage_stops_next_step_truthfully(
    tmp_path: Path, code, expected
) -> None:
    first, second = attempt_start(), attempt_start(index=2)
    failure = ModelAttemptFinished(
        attempt_id=first.attempt_id,
        outcome="failed",
        error=AgentFailure(code=code, message="服务失败", retryable=True),
    )
    turn, tools = await execute(
        tmp_path,
        [first, failure, *_tool_answer(second)],
        budget=Budget(max_tokens=6_000_000, max_output_chars=1_024),
    )
    # 第一步重试成功并执行了工具；下一步发起前因用量未知停止。
    assert tools.calls and [a.status for a in turn.model_attempts] == ["failed", "completed"]
    assert turn.status == "failed" and turn.error.code == expected
    assert turn.error.category == ("budget" if expected == "budget_exceeded" else "provider")


@pytest.mark.parametrize("with_observation", [False, True])
async def test_failed_stream_keeps_usage_and_settles_open_attempt(
    tmp_path: Path, with_observation
) -> None:
    start = attempt_start()
    events = [start, ResponseStarted(response_id="response-1")]
    if with_observation:
        events.append(observed(start, completeness="partial", input_tokens=10, output_tokens=2))
    events.append(ResponseFailed(code="transport", retryable=True))
    turn, _ = await execute(tmp_path, events)
    assert turn.status == "failed"
    attempt = turn.model_attempts[0]
    assert attempt.status == "failed" and attempt.error == turn.error
    assert attempt.usage.completeness == ("partial" if with_observation else "unknown")
    assert attempt.usage.input_tokens == (10 if with_observation else None)
    assert turn.usage.total_tokens == (12 if with_observation else 0)
    assert not turn.usage_is_complete


@pytest.mark.parametrize("cause", ["cancel_token", "task_cancel", "timeout", "exception", "eof"])
async def test_interrupted_stream_is_settled_by_kernel_not_generator_cleanup(
    tmp_path: Path, cause, monkeypatch
) -> None:
    deadlines = capture_deadlines(monkeypatch, runtime_module)
    store = SQLiteSessionStore(tmp_path / "s.db")
    entered, closed = asyncio.Event(), asyncio.Event()
    start = attempt_start()

    class Provider:
        async def stream(self, request, cancel):
            try:
                yield start
                yield observed(start, completeness="partial", input_tokens=10, output_tokens=1)
                entered.set()
                if cause == "exception":
                    raise RuntimeError("不应该持久化的原始异常")
                if cause == "eof":
                    return
                await asyncio.Event().wait()
            finally:
                closed.set()

    async with AgentRuntime(store, Provider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(
            runtime.run_turn(
                thread.thread_id,
                "停止测试",
                request_id="r",
                budget=Budget(timeout_seconds=10),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        if cause == "timeout":
            deadlines[-1].reschedule(asyncio.get_running_loop().time())
        if cause == "cancel_token":
            turn_id = (await store.get_thread(thread.thread_id)).active_turn_id
            await runtime.cancel(thread.thread_id, turn_id)
        if cause == "task_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 2)
        turn = (await store.get_thread(thread.thread_id)).turns[-1]
    assert closed.is_set()
    assert turn.usage.total_tokens == 11
    assert turn.model_attempts[0].usage.completeness == "partial"
    assert turn.model_attempts[0].status == (
        "cancelled" if cause in {"cancel_token", "task_cancel"} else "failed"
    )
    assert "不应该持久化" not in turn.model_dump_json()
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize(
    "case",
    [
        "no_start",
        "wrong_attempt",
        "wrong_step",
        "skipped_index",
        "parallel",
        "retry_after_response",
        "success_without_usage",
        "wrong_response_usage",
        "response_before_finish",
        "duplicate_finish",
        "changed_model",
        "rewind",
        "usage_after_finish",
        "retry_after_success",
        "post_response_metadata",
    ],
)
async def test_invalid_attempt_protocol_never_executes_tools(tmp_path: Path, case) -> None:
    start = attempt_start()
    full = observed(start, completeness="complete", input_tokens=10, output_tokens=3)
    finish = ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed")
    cases = {
        "no_start": [full],
        "wrong_attempt": [start, full.model_copy(update={"attempt_id": new_id()})],
        "wrong_step": [start.model_copy(update={"step": 2})],
        "skipped_index": [start.model_copy(update={"index": 2})],
        "parallel": [start, attempt_start(index=2)],
        "retry_after_response": [
            start,
            ResponseStarted(response_id="r"),
            failed(start),
            attempt_start(index=2),
        ],
        "success_without_usage": [start, finish],
        "wrong_response_usage": [*accounted_answer(start=start)[:-1], ResponseCompleted()],
        "response_before_finish": [start, *answer()],
        "duplicate_finish": [start, full, finish, finish],
        "changed_model": [start, full, full.model_copy(update={"actual_model": "other"})],
        "rewind": [
            start,
            full,
            observed(start, completeness="complete", input_tokens=9, output_tokens=3),
        ],
        "usage_after_finish": [start, full, finish, full],
        "retry_after_success": [start, full, finish, attempt_start(index=2)],
        "post_response_metadata": [*accounted_answer(start=start), full],
    }
    turn, tools = await execute(tmp_path, [*cases[case], *tool_step("test.read")])
    assert turn.status == "failed"
    assert turn.error.code == "invalid_provider_output"
    assert tools.calls == []


async def test_mixed_steps_use_one_budget_without_injecting_receipts_into_history(
    tmp_path: Path,
) -> None:
    first = tool_step("test.read")
    first[-1] = ResponseCompleted(
        finish_reason="tool_calls", usage=Usage(input_tokens=5, output_tokens=1)
    )
    provider = ScriptedProvider([first, accounted_answer(start=attempt_start(step=2))])
    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = RecordingTools()
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "混合 Provider",
            request_id="r",
            budget=Budget(max_tokens=50_000, max_output_chars=1_024),
        )
    assert turn.usage == Usage(input_tokens=15, output_tokens=4)
    assert provider.requests[1].remaining_tokens == 1_024
    assert len(tools.calls) == 1 and turn.usage_step == 2
    assert not turn.usage_is_complete
    assert all(
        item.content.kind in {"user_message", "tool_call", "tool_result"}
        for item in provider.requests[1].history
    )


async def test_complete_usage_on_unsuccessful_response_is_not_lost(tmp_path: Path) -> None:
    events = accounted_answer()
    events[-1] = ResponseCompleted(
        finish_reason="max_output_tokens", usage=Usage(input_tokens=10, output_tokens=3)
    )
    turn, _ = await execute(tmp_path, events)
    assert turn.status == "failed" and turn.error.code == "provider_max_output_tokens"
    assert turn.usage.total_tokens == 13 and turn.usage_is_complete
    assert turn.model_attempts[0].status == "completed"


async def test_attempt_ids_cannot_be_reused_in_followup_turn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    provider = ScriptedProvider([accounted_answer()])
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        first = await runtime.run_turn(thread.thread_id, "第一轮", request_id="first")
        second = await runtime.run_turn(thread.thread_id, "第二轮", request_id="second")
    assert first.status == "completed"
    assert second.status == "failed" and second.error.code == "invalid_provider_output"
    assert second.model_attempts == ()


@pytest.mark.parametrize("usage_first", [False, True])
async def test_response_identity_must_match_attempt(tmp_path: Path, usage_first) -> None:
    start = attempt_start()
    observation = observed(start, completeness="partial", input_tokens=10)
    response = ResponseStarted(response_id="different-response")
    events = [start, observation, response] if usage_first else [start, response, observation]
    turn, _ = await execute(tmp_path, events)
    assert turn.error.code == "invalid_provider_output"
    assert turn.usage.input_tokens == (10 if usage_first else 0)


async def test_attempt_event_idempotency_and_tool_gate(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepare_attempt(store, tmp_path)
    start = attempt_start()
    drafts = [
        EventDraft(turn_id=thread.active_turn_id, payload=p)
        for p in [start, observed(start, completeness="partial", input_tokens=10, output_tokens=1)]
    ]
    updated = await store.append(thread.thread_id, drafts, expected_sequence=thread.sequence)
    assert (
        await store.append(thread.thread_id, drafts, expected_sequence=thread.sequence) == updated
    )
    assert updated.turns[-1].usage.total_tokens == 11
    assert len(updated.turns[-1].model_attempts) == 1


async def test_accounted_tool_loop_checks_known_budget_on_next_step(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    first, second = attempt_start(), attempt_start(step=2)
    tool_events = tool_step("test.read")
    tool_events[0] = ResponseStarted(response_id="response-1")
    tool_events[-1] = ResponseCompleted(
        finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=3)
    )
    first_step = [
        first,
        *tool_events[:-1],
        observed(first, completeness="complete", input_tokens=10, output_tokens=3),
        ModelAttemptFinished(attempt_id=first.attempt_id, outcome="completed"),
        tool_events[-1],
    ]
    provider = ScriptedProvider([first_step, accounted_answer(start=second)])
    tools = RecordingTools()
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "工具闭环",
            request_id="r",
            budget=Budget(max_tokens=50_000, max_output_chars=1_024),
        )
    assert turn.status == "completed" and turn.usage_is_complete
    assert turn.usage == Usage(input_tokens=20, output_tokens=6)
    assert provider.requests[1].remaining_tokens == 1_024
    assert len(tools.calls) == 1
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
