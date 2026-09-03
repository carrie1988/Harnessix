from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from harnessix.agent.models import Budget, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelUsageObserved
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.models.attempt_helpers import CANARY, KEY_ENV, Adapter


@pytest.fixture(params=["openai", "anthropic"])
def adapter(request, monkeypatch):
    monkeypatch.setenv(KEY_ENV, CANARY)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    return Adapter(request.param)


async def execute(adapter, tmp_path, parts, *, fail=False, block=False, **options):
    store = SQLiteSessionStore(tmp_path / "s.db")
    wire = adapter.wire.WireStream(parts, fail=fail, block=block)
    tools = RecordingTools()
    requests = []

    async def handle(request):
        requests.append(request)
        thread = await store.get_thread(thread_id)
        assert thread.turns[-1].model_attempts[-1].status == "running"
        return adapter.wire.response(wire)

    async with adapter.provider(wire, handler=handle, max_attempts=1, **options) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            thread_id = thread.thread_id
            turn = await runtime.run_turn(thread_id, "模型用量测试", request_id="r")
    events = await store.events(thread_id)
    assert replay(events) == await store.get_thread(thread_id)
    assert CANARY not in repr(events)
    assert len(requests) == 1 and wire.closed
    assert len(turn.model_attempts) == 1 and turn.model_attempts[0].status != "running"
    return turn, events, tools


async def test_native_usage_details_and_alias_identity(adapter, tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.INFO):
        turn, events, _ = await execute(adapter, tmp_path, adapter.detailed_frames())
    assert turn.status == "completed" and turn.usage_is_complete
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)
    attempt = turn.model_attempts[0]
    assert attempt.requested_model == "requested-model" and attempt.actual_model == "test-model"
    assert attempt.usage.cache_read_input_tokens == 4
    assert attempt.usage.cache_creation_input_tokens == 3
    assert attempt.usage.uncached_input_tokens == 3
    assert attempt.usage.reasoning_output_tokens == 1
    observations = [e.payload for e in events if isinstance(e.payload, ModelUsageObserved)]
    assert observations[-1].usage.completeness == "complete"
    assert all(e.attempt_id == attempt.attempt_id for e in observations)
    assert CANARY not in caplog.text


async def test_missing_counters_remain_unknown(adapter, tmp_path: Path) -> None:
    parts = adapter.wire.text_frames()
    if adapter.kind == "openai":
        value = adapter.wire.chunk(usage=True)
        value["usage"]["prompt_tokens_details"] = {"cached_tokens": 4}
        parts[-2] = adapter.wire.frame(value)
    else:
        parts[0] = adapter.wire.start(cache_creation_input_tokens=None)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    observation = turn.model_attempts[0].usage
    assert observation.cache_creation_input_tokens is None
    assert observation.reasoning_output_tokens is None
    if adapter.kind == "openai":
        assert turn.status == "completed" and observation.input_tokens == 10
        assert observation.uncached_input_tokens is None
    else:
        assert turn.status == "failed" and observation.input_tokens is None
        assert observation.uncached_input_tokens == 10
        assert observation.completeness == "partial" and turn.usage.output_tokens == 2


@pytest.mark.parametrize(
    "point", ["before_response", "after_start", "after_usage", "bad_arguments", "truncated"]
)
async def test_failure_preserves_last_valid_observation(adapter, tmp_path: Path, point) -> None:
    parts = adapter.wire.text_frames()
    failing = point not in {"bad_arguments", "truncated"}
    if point == "before_response":
        parts = []
    elif point == "after_start":
        parts = parts[:1]
    elif point == "bad_arguments":
        parts = adapter.wire.tool_frames("{")
    elif point == "truncated" or adapter.kind == "openai":
        parts = parts[:-1]
    turn, _, tools = await execute(adapter, tmp_path, parts, fail=failing)
    attempt = turn.model_attempts[0]
    assert turn.status == attempt.status == "failed"
    assert attempt.error == turn.error
    assert tools.calls == []
    expected = "complete"
    if point == "before_response" or (adapter.kind == "openai" and point == "after_start"):
        expected = "unknown"
    elif adapter.kind == "anthropic" and point in {"after_start", "truncated"}:
        expected = "partial"
    assert attempt.usage.completeness == expected
    if expected == "unknown":
        assert attempt.usage.input_tokens is None and attempt.usage.output_tokens is None
        assert turn.usage.total_tokens == 0 and not turn.usage_is_complete
    else:
        assert turn.usage.input_tokens == 10
        assert turn.usage.output_tokens == (1 if point == "after_start" else 2)


@pytest.mark.parametrize("reason", ["max_tokens", "context_overflow"])
async def test_unsuccessful_stop_keeps_complete_usage(adapter, tmp_path: Path, reason) -> None:
    parts = adapter.wire.text_frames()
    if adapter.kind == "openai":
        if reason == "max_tokens":
            parts[-3] = adapter.wire.frame(adapter.wire.chunk(finish="length"))
        else:
            parts[-1] = adapter.wire.frame(
                {"error": {"code": "context_length_exceeded", "message": CANARY}}
            )
    else:
        parts[-2:] = adapter.wire.stop(
            "max_tokens" if reason == "max_tokens" else "model_context_window_exceeded"
        )
    turn, _, _ = await execute(adapter, tmp_path, parts)
    assert turn.status == "failed" and turn.usage_is_complete
    assert turn.usage.total_tokens == 12
    attempt = turn.model_attempts[0]
    assert attempt.status == ("completed" if reason == "max_tokens" else "failed")
    assert turn.error.code == (
        "provider_max_output_tokens" if reason == "max_tokens" else "provider_context_overflow"
    )


async def test_retry_receipts_do_not_disable_retry_or_double_count(adapter, tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    wire = adapter.wire.WireStream(adapter.wire.text_frames())
    requests = []

    async def handle(request):
        requests.append(request)
        thread = await store.get_thread(thread_id)
        assert len(thread.turns[-1].model_attempts) == len(requests)
        assert thread.turns[-1].model_attempts[-1].status == "running"
        return adapter.error() if len(requests) == 1 else adapter.wire.response(wire)

    async with adapter.provider(wire, handler=handle) as provider:
        async with AgentRuntime(store, provider) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            thread_id = thread.thread_id
            turn = await runtime.run_turn(thread_id, "重试", request_id="r")
    assert turn.status == "completed" and len(requests) == 2
    first, second = turn.model_attempts
    assert first.attempt_id != second.attempt_id and (first.index, second.index) == (1, 2)
    assert first.usage.completeness == "unknown" and first.usage.input_tokens is None
    assert second.usage.completeness == "complete"
    assert turn.usage.total_tokens == 12 and not turn.usage_is_complete
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)


@pytest.mark.parametrize("point", ["before_response", "after_start", "after_usage"])
@pytest.mark.parametrize("method", ["cancel_token", "task_cancel"])
async def test_sdk_cancellation_preserves_durable_usage(
    adapter, tmp_path: Path, point, method
) -> None:
    parts = adapter.wire.text_frames()
    if point == "before_response":
        parts = []
    elif point == "after_start":
        parts = parts[:1]
    elif adapter.kind == "openai":
        parts.pop()
    wire = adapter.wire.WireStream(parts, block=True)
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with adapter.provider(wire) as provider:
        async with AgentRuntime(store, provider) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            task = asyncio.create_task(
                runtime.run_turn(
                    thread.thread_id, "取消", request_id="r", budget=Budget(timeout_seconds=10)
                )
            )
            await asyncio.wait_for(wire.entered.wait(), 3)
            if method == "task_cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                snapshot = await store.get_thread(thread.thread_id)
                await runtime.cancel(thread.thread_id, snapshot.active_turn_id)
                await asyncio.wait_for(task, 3)
            turn = (await store.get_thread(thread.thread_id)).turns[-1]
    assert wire.closed and turn.status == "cancelled"
    assert len(turn.model_attempts) == 1
    attempt = turn.model_attempts[0]
    assert attempt.status == "cancelled"
    if point == "after_usage":
        assert attempt.usage.completeness == "complete" and turn.usage.total_tokens == 12
    elif adapter.kind == "anthropic" and point == "after_start":
        assert attempt.usage.completeness == "partial" and turn.usage.total_tokens == 11
    else:
        assert attempt.usage.completeness == "unknown" and attempt.usage.input_tokens is None
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("value", [True, "1", 1.0, -1])
@pytest.mark.parametrize("field", ["cache", "reasoning"])
async def test_invalid_detail_cannot_overwrite_prior_usage(
    adapter, tmp_path: Path, value, field
) -> None:
    parts = adapter.wire.text_frames()
    if adapter.kind == "openai":
        event = adapter.wire.chunk(usage=True)
        event["usage"][
            "prompt_tokens_details" if field == "cache" else "completion_tokens_details"
        ] = {"cached_tokens" if field == "cache" else "reasoning_tokens": value}
        parts[-2] = adapter.wire.frame(event)
    else:
        data = (
            {"cache_read_input_tokens": value}
            if field == "cache"
            else {"output_tokens_details": {"thinking_tokens": value}}
        )
        parts[-2:] = adapter.wire.stop(**data)
    turn, _, tools = await execute(adapter, tmp_path, parts)
    assert turn.status == "failed" and turn.error.code == "provider_invalid_provider_output"
    assert not tools.calls
    if adapter.kind == "openai":
        assert turn.model_attempts[0].usage.completeness == "unknown"
    else:
        assert turn.usage == Usage(input_tokens=10, output_tokens=1)
        assert turn.model_attempts[0].usage.reasoning_output_tokens is None


async def test_sdk_timeout_after_usage_does_not_erase_receipt(adapter, tmp_path: Path) -> None:
    parts = adapter.wire.text_frames()
    if adapter.kind == "openai":
        parts.pop()
    turn, _, _ = await execute(adapter, tmp_path, parts, block=True, timeout_seconds=1)
    assert turn.status == "failed" and turn.error.code == "provider_transport"
    assert turn.usage_is_complete and turn.usage.total_tokens == 12


async def test_duplicate_cumulative_report_is_not_added_twice(adapter, tmp_path: Path) -> None:
    parts = adapter.wire.text_frames()
    duplicate = parts[-2]
    if adapter.kind == "anthropic":
        value = json.loads(duplicate.split(b"data: ", 1)[1])
        value["delta"]["stop_reason"] = None
        duplicate = adapter.wire.frame("message_delta", delta=value["delta"], usage=value["usage"])
    parts.insert(-1, duplicate)
    turn, events, _ = await execute(adapter, tmp_path, parts)
    assert turn.status == ("failed" if adapter.kind == "openai" else "completed")
    assert turn.usage.total_tokens == 12
    if adapter.kind == "anthropic":
        assert sum(isinstance(e.payload, ModelUsageObserved) for e in events) == 3


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
@pytest.mark.parametrize(
    "case", ["cache_read", "cache_write", "cache_sum", "reasoning", "boolean", "model_drift"]
)
async def test_chat_rejects_inconsistent_totals_and_identity(adapter, tmp_path: Path, case) -> None:
    parts = adapter.wire.text_frames()
    value = adapter.wire.chunk(usage=True)
    if case == "model_drift":
        value["model"] = "different-model"
    elif case == "boolean":
        value["usage"].update(prompt_tokens=True, total_tokens=3)
    elif case == "reasoning":
        value["usage"]["completion_tokens_details"] = {"reasoning_tokens": 3}
    else:
        value["usage"]["prompt_tokens_details"] = {
            "cache_read": {"cached_tokens": 100},
            "cache_write": {"cache_write_tokens": 100},
            "cache_sum": {"cached_tokens": 6, "cache_write_tokens": 5},
        }[case]
    parts[-2] = adapter.wire.frame(value)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    assert turn.status == "failed" and turn.error.code == "provider_invalid_provider_output"
    assert turn.model_attempts[0].usage.completeness == "unknown"
    assert turn.model_attempts[0].actual_model == "test-model"


@pytest.mark.parametrize("adapter", ["anthropic"], indirect=True)
@pytest.mark.parametrize("late_cache", [False, True])
async def test_anthropic_cache_growth_and_late_detail_are_valid(
    adapter, tmp_path: Path, late_cache
) -> None:
    parts = adapter.wire.text_frames()
    parts[0] = adapter.wire.start(
        cache_read_input_tokens=None if late_cache else 0,
        output_tokens_details={"thinking_tokens": 1},
    )
    parts[-2:] = adapter.wire.stop(cache_read_input_tokens=100)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    assert turn.status == "completed" and turn.usage_is_complete
    assert turn.usage == Usage(input_tokens=110, output_tokens=2)
    observation = turn.model_attempts[0].usage
    assert observation.uncached_input_tokens == 10 and observation.cache_read_input_tokens == 100
    assert observation.reasoning_output_tokens == 1


@pytest.mark.parametrize("adapter", ["anthropic"], indirect=True)
@pytest.mark.parametrize("thinking", [0, 3])
async def test_anthropic_invalid_candidate_keeps_prior_detail(
    adapter, tmp_path: Path, thinking
) -> None:
    parts = adapter.wire.text_frames()
    parts[0] = adapter.wire.start(output_tokens_details={"thinking_tokens": 1})
    parts[-2:] = adapter.wire.stop(
        cache_read_input_tokens=100, output_tokens_details={"thinking_tokens": thinking}
    )
    turn, _, _ = await execute(adapter, tmp_path, parts)
    assert turn.status == "failed" and turn.error.code == "provider_invalid_provider_output"
    assert turn.usage == Usage(input_tokens=10, output_tokens=1)
    observation = turn.model_attempts[0].usage
    assert observation.cache_read_input_tokens == 0 and observation.reasoning_output_tokens == 1
