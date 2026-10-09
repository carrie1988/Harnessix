"""流阶段明确拒绝的低敏原因；保持原范围、零工具释放及持久回放。"""

from __future__ import annotations

import logging

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Budget
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models.config import ChatCapabilities
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.contracts.provider import model_request
from tests.models.test_openai_chat import config
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames, tool_frames

CANARY = "PRIVATE_STREAM_VALUE_NEVER_IN_DIAGNOSTIC"
SCENARIOS = [
    ("first_event_name", "frame_event_unsupported"),
    ("first_frame_schema", "frame_schema_invalid"),
    ("event_name", "frame_event_unsupported"),
    ("frame_schema", "frame_schema_invalid"),
    ("response_id", "response_identity_changed"),
    ("model_identity", "response_identity_changed"),
    ("after_usage", "chunk_after_usage"),
    ("early_usage", "usage_shape_or_order_invalid"),
    ("usage_choices", "usage_shape_or_order_invalid"),
    ("usage_total", "usage_shape_or_order_invalid"),
    ("billing_drift", "billing_metadata_invalid"),
    ("cache_overflow", "usage_details_invalid"),
    ("negative_cache", "usage_details_invalid"),
    ("cache_partition_overflow", "usage_details_invalid"),
    ("reasoning_overflow", "usage_details_invalid"),
    ("empty_choices", "choice_shape_or_order_invalid"),
    ("choice_index", "choice_shape_or_order_invalid"),
    ("after_finish", "choice_shape_or_order_invalid"),
    ("role", "message_type_unsupported"),
    ("legacy_function", "message_type_unsupported"),
    ("tool_index", "tool_index_limit_exceeded"),
    ("parallel", "parallel_tool_calls_disabled"),
    ("tool_id", "tool_id_changed"),
    ("tool_name", "tool_name_changed"),
    ("output_limit", "output_char_limit_exceeded"),
    ("long_tool_id", "tool_id_limit_exceeded"),
]


def rejected_frames(scenario):
    first = frame(chunk())
    bad = chunk()
    if scenario in {"event_name", "first_event_name"}:
        invalid = b"event: " + CANARY.encode() + b"\n" + frame(bad)
        return [invalid] if scenario == "first_event_name" else [first, invalid]
    if scenario in {"frame_schema", "first_frame_schema"}:
        bad["choices"][0]["index"] = CANARY
        if scenario == "first_frame_schema":
            return [frame(bad)]
    elif scenario == "response_id":
        bad["id"] = CANARY
    elif scenario == "model_identity":
        bad["model"] = CANARY
    elif scenario == "after_usage":
        return [*text_frames()[:-1], frame(bad)]
    elif scenario == "early_usage":
        return [first, frame(chunk(usage=True))]
    elif scenario in {"usage_choices", "usage_total"}:
        bad = chunk(usage=True)
        if scenario == "usage_choices":
            bad["choices"] = chunk()["choices"]
        else:
            bad["usage"]["total_tokens"] = 999
        return [first, frame(chunk(finish="stop")), frame(bad)]
    elif scenario == "billing_drift":
        before = chunk()
        before["service_tier"] = "default"
        bad["service_tier"] = "priority"
        return [frame(before), frame(bad)]
    elif scenario in {
        "cache_overflow",
        "negative_cache",
        "cache_partition_overflow",
        "reasoning_overflow",
    }:
        bad = chunk(usage=True)
        details = {
            "cache_overflow": {"cached_tokens": 11},
            "negative_cache": {"cached_tokens": -1},
            "cache_partition_overflow": {"cached_tokens": 6, "cache_write_tokens": 5},
        }
        if scenario == "reasoning_overflow":
            bad["usage"]["completion_tokens_details"] = {"reasoning_tokens": 3}
        else:
            bad["usage"]["prompt_tokens_details"] = details[scenario]
        return [first, frame(chunk(finish="stop")), frame(bad)]
    elif scenario == "empty_choices":
        bad["choices"] = []
    elif scenario == "choice_index":
        bad["choices"][0]["index"] = 1
    elif scenario == "after_finish":
        return [first, frame(chunk(finish="stop")), frame(bad)]
    elif scenario == "role":
        bad = chunk({"role": "user", "content": CANARY})
    elif scenario == "legacy_function":
        bad = chunk({"function_call": {"name": CANARY, "arguments": "{}"}})
    elif scenario == "tool_index":
        bad = chunk({"tool_calls": [call(1000)]})
    elif scenario == "parallel":
        bad = chunk({"tool_calls": [call(0), call(1)]})
    elif scenario in {"tool_id", "tool_name"}:
        changed = call(arguments="")
        if scenario == "tool_id":
            changed["id"] = CANARY
        else:
            changed["function"]["name"] = CANARY
        return [first, tool_frames()[0], frame(chunk({"tool_calls": [changed]}))]
    elif scenario == "output_limit":
        bad = chunk({"content": CANARY})
    elif scenario == "long_tool_id":
        changed = call()
        changed["id"] = CANARY + "x" * 256
        return [frame(chunk({"tool_calls": [changed]})), *tool_frames()[1:]]
    else:
        raise AssertionError("未登记场景")
    return [first, frame(bad)]


@pytest.mark.parametrize("scenario,reason", SCENARIOS)
async def test_existing_stream_rejection_retains_one_attempt_and_no_tools(scenario, reason, caplog):
    wire = WireStream(rejected_frames(scenario))
    sent = 0

    def handle(request):
        nonlocal sent
        sent += 1
        return response(wire)

    request = model_request(with_tools=True)
    if scenario == "output_limit":
        request = request.model_copy(update={"budget": Budget(max_output_chars=1)})
    with caplog.at_level(logging.INFO):
        async with OpenAIChatProvider(
            config(
                max_attempts=3,
                capabilities=ChatCapabilities(parallel_tool_calls=scenario != "parallel"),
            ),
            transport=httpx.MockTransport(handle),
        ) as provider:
            events = [e async for e in provider.stream(request, CancelToken())]
    assert sent == 1 and wire.closed
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert sum(isinstance(e, ModelAttemptStarted) for e in events) == 1
    ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert len(ends) == 1 and ends[0].outcome == "failed"
    failure = ends[0].error
    assert (failure.code, failure.category, failure.retryable) == (
        "provider_invalid_provider_output",
        "provider",
        False,
    )
    assert failure.message == f"Provider 返回结构化失败；chat_protocol/v1:{reason}"
    assert not any(
        isinstance(e, ToolCallCompleted | ToolCallRejected | ResponseCompleted) for e in events
    )
    observations = [e for e in events if isinstance(e, ModelUsageObserved)]
    # 只保留先前已通过校验的用量；非法明细不能被诊断代码变成可结算事实。
    if scenario.startswith("first_"):
        assert observations == []
    else:
        complete = scenario in {"after_usage", "long_tool_id"}
        assert observations[-1].usage.completeness == ("complete" if complete else "unknown")
    assert CANARY not in repr(events) and CANARY not in caplog.text


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("frame_schema", "frame_schema_invalid"),
        ("after_usage", "chunk_after_usage"),
        ("parallel", "parallel_tool_calls_disabled"),
    ],
)
async def test_stream_diagnostic_persists_and_replays_without_model_retry(
    tmp_path, scenario, reason
):
    wire = WireStream(rejected_frames(scenario))
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    tools = RecordingTools()
    async with OpenAIChatProvider(
        config(max_attempts=3, capabilities=ChatCapabilities(parallel_tool_calls=False)),
        transport=httpx.MockTransport(lambda request: response(wire)),
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "调查登录链", request_id="stream-check")
    assert turn.status == "failed" and turn.model_steps == 1
    assert len(turn.model_attempts) == 1 and tools.calls == [] and wire.closed
    assert turn.model_attempts[0].error.message.endswith("chat_protocol/v1:" + reason)
    reopened = SQLiteSessionStore(store.path)
    events = await reopened.events(thread.thread_id)
    restored = await reopened.get_thread(thread.thread_id)
    assert replay(events) == restored and restored.turns[-1] == turn
    assert CANARY not in repr(events)
