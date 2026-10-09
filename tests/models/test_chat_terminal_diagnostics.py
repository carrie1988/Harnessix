"""终态诊断只记录封闭原因；保留失败、用量、零工具释放与会话回放。"""

from __future__ import annotations

import logging
from uuid import uuid4

import httpx
import pytest
from openai import APIConnectionError

from harnessix.agent.models import (
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Usage,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason, diagnostic_failure
from harnessix.models._provider_io import finish_attempt
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.models.test_openai_chat import collect, config
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames, tool_frames

IDENTITY_CANARY = "PRIVATE_TOOL_IDENTITY_NEVER_IN_DIAGNOSTIC"


def malformed_frames(scenario):
    parts = tool_frames()
    item = call()
    if scenario == "missing_id":
        item["id"] = ""
    elif scenario == "duplicate_id":
        second = call(index=1)
        second["id"] = item["id"]
        parts[0] = frame(chunk({"tool_calls": [item, second]}))
        return parts
    elif scenario == "unknown_tool":
        item["function"]["name"] = IDENTITY_CANARY
    elif scenario == "missing_name":
        item["function"].pop("name")
    elif scenario == "missing_type":
        item.pop("type")
    elif scenario == "index_gap":
        item["index"] = 1
    elif scenario == "second_tool_invalid":
        second = call(index=1)
        second["function"]["name"] = IDENTITY_CANARY
        parts[0] = frame(chunk({"tool_calls": [item, second]}))
        return parts
    elif scenario in {"bad_json", "duplicate_key", "non_finite", "not_object"}:
        return tool_frames(
            {
                "bad_json": "{",
                "duplicate_key": '{"a":1,"a":2}',
                "non_finite": '{"a":NaN}',
                "not_object": "[]",
            }[scenario]
        )
    elif scenario == "empty_response":
        return [frame(chunk()), frame(chunk(finish="stop")), frame(chunk(usage=True)), parts[-1]]
    elif scenario == "no_usage":
        return [parts[0], parts[1], parts[-1]]
    elif scenario == "no_done":
        return parts[:-1]
    elif scenario == "finish_mismatch":
        parts[1] = frame(chunk(finish="stop"))
        return parts
    elif scenario == "unsupported_finish":
        parts[1] = frame(chunk(finish="function_call"))
        return parts
    else:
        raise AssertionError("测试场景未登记")
    parts[0] = frame(chunk({"tool_calls": [item]}))
    return parts


SCENARIOS = [
    ("missing_id", "tool_id_missing"),
    ("duplicate_id", "tool_id_duplicate"),
    ("unknown_tool", "tool_name_unknown"),
    ("missing_name", "tool_name_unknown"),
    ("missing_type", "tool_type_invalid"),
    ("index_gap", "tool_index_gap"),
    ("second_tool_invalid", "tool_name_unknown"),
    ("bad_json", "tool_arguments_invalid"),
    ("duplicate_key", "tool_arguments_invalid"),
    ("non_finite", "tool_arguments_invalid"),
    ("not_object", "tool_arguments_not_object"),
    ("empty_response", "semantic_output_missing"),
    ("no_usage", "completion_incomplete"),
    ("no_done", "completion_incomplete"),
    ("finish_mismatch", "finish_tool_mismatch"),
    ("unsupported_finish", "finish_reason_unsupported"),
]


@pytest.mark.parametrize("supply_late_name", [False, True])
async def test_deferred_name_must_arrive_before_batch_completion(supply_late_name):
    first, second = call(), call(index=1)
    second["function"].pop("name")
    parts = [frame(chunk({"tool_calls": [first, second]}))]
    if supply_late_name:
        parts.append(
            frame(
                chunk(
                    {"tool_calls": [{"index": 1, "function": {"name": first["function"]["name"]}}]}
                )
            )
        )
    parts.extend([frame(chunk(finish="tool_calls")), frame(chunk(usage=True)), b"data: [DONE]\n\n"])
    events, wire = await collect(parts, max_attempts=3)
    observations = [e for e in events if isinstance(e, ModelUsageObserved)]
    assert observations[-1].usage.completeness == "complete"
    assert wire.closed and sum(isinstance(e, ModelAttemptStarted) for e in events) == 1
    calls = [e for e in events if isinstance(e, ToolCallCompleted | ToolCallRejected)]
    if supply_late_name:
        assert len(calls) == 2 and all(isinstance(e, ToolCallCompleted) for e in calls)
        assert isinstance(events[-1], ResponseCompleted)
    else:
        assert calls == [] and events[-1] == ResponseFailed(code="invalid_provider_output")
        finished = next(e for e in events if isinstance(e, ModelAttemptFinished))
        assert finished.error.message.endswith("chat_protocol/v1:tool_name_unknown")


@pytest.mark.parametrize("scenario,reason", SCENARIOS)
async def test_sdk_terminal_diagnostic_preserves_failure_and_releases_no_tools(scenario, reason):
    """保留全部结构负控；完整的目录外成员仅产生闭合拒绝，不伪造协议诊断。"""
    events, wire = await collect(malformed_frames(scenario), max_attempts=3)
    starts = [e for e in events if isinstance(e, ModelAttemptStarted)]
    ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert len(starts) == len(ends) == 1
    assert ends[0].attempt_id == starts[0].attempt_id
    if scenario in {"unknown_tool", "second_tool_invalid"}:
        expected = []
        if scenario == "second_tool_invalid":
            expected.append(ToolCallCompleted(call_id=call()["id"], tool="test.read", arguments={}))
        expected.append(
            ToolCallRejected(
                call_id=call(index=1 if scenario == "second_tool_invalid" else 0)["id"],
                argument_chars=2,
            )
        )
        assert [
            e for e in events if isinstance(e, ToolCallCompleted | ToolCallRejected)
        ] == expected
        assert events[-1] == ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=2)
        )
        assert ends[0].outcome == "completed" and ends[0].error is None
    else:
        assert events[-1] == ResponseFailed(code="invalid_provider_output")
        assert not any(
            isinstance(e, ToolCallCompleted | ToolCallRejected | ResponseCompleted) for e in events
        )
        assert ends[0].outcome == "failed"
        assert ends[0].error.code == "provider_invalid_provider_output"
        assert not ends[0].error.retryable and ends[0].error.category == "provider"
        assert ends[0].error.message == f"Provider 返回结构化失败；chat_protocol/v1:{reason}"
    assert IDENTITY_CANARY not in repr(events)
    assert wire.closed
    if scenario != "no_usage":
        observations = [e for e in events if isinstance(e, ModelUsageObserved)]
        assert observations[-1].usage.completeness == "complete"
        assert observations[-1].usage.input_tokens == 10
        assert observations[-1].usage.output_tokens == 2


@pytest.mark.parametrize(
    "scenario", ["unknown_tool", "second_tool_invalid", "missing_name", "bad_json", "no_done"]
)
async def test_sdk_failed_attempt_diagnostic_persists_and_replays_without_execution(
    tmp_path, scenario, caplog
):
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    wire = WireStream(malformed_frames(scenario))
    correction = WireStream(text_frames())
    tools = RecordingTools()
    requests = []

    def handle(request):
        requests.append(request)
        return response(wire if len(requests) == 1 else correction)

    with caplog.at_level(logging.INFO):
        async with OpenAIChatProvider(
            config(max_attempts=3), transport=httpx.MockTransport(handle)
        ) as provider:
            async with AgentRuntime(store, provider, tools) as runtime:
                thread = await runtime.create_thread(str(tmp_path))
                turn = await runtime.run_turn(thread.thread_id, "协议失败诊断", request_id="r")
    if scenario in {"unknown_tool", "second_tool_invalid"}:
        assert turn.status == "completed" and turn.error is None and turn.model_steps == 2
        assert turn.usage == Usage(input_tokens=20, output_tokens=4)
        assert len(turn.model_attempts) == 2 and all(
            attempt.status == "completed" and attempt.error is None
            for attempt in turn.model_attempts
        )
        rejected = [
            item.content
            for item in turn.items
            if isinstance(item.content, ToolCallRejectionContent)
        ]
        assert len(rejected) == 1 and rejected[0].reason == "unregistered_tool"
        assert rejected[0].model_step == 1
        rejected_results = [
            item.content
            for item in turn.items
            if isinstance(item.content, ToolResultContent)
            and item.content.call_id == rejected[0].call_id
        ]
        assert len(rejected_results) == 1
        result = rejected_results[0]
        assert result.outcome == "failed" and result.output is None
        assert result.error.model_dump() == {
            "code": "unknown_tool",
            "message": "工具未注册",
            "retryable": False,
            "category": "tool",
        }
        assert all(
            getattr(result, field) is None
            for field in (
                "action_id",
                "patch",
                "patch_batch",
                "process",
                "trusted_action",
                "diff_artifact",
            )
        )
        assert len(requests) == 2 and wire.closed and correction.closed
        assert len(tools.calls) == (1 if scenario == "second_tool_invalid" else 0)
        assert all(invoked.tool == "test.read" for invoked in tools.calls)
    else:
        assert turn.status == "failed" and turn.error.code == "provider_invalid_provider_output"
        assert turn.usage == Usage(input_tokens=10, output_tokens=2)
        assert len(turn.model_attempts) == 1 and turn.model_attempts[0].status == "failed"
        expected_reason = {
            "bad_json": "tool_arguments_invalid",
            "missing_name": "tool_name_unknown",
            "no_done": "completion_incomplete",
        }[scenario]
        assert turn.model_attempts[0].error.message == (
            f"Provider 返回结构化失败；chat_protocol/v1:{expected_reason}"
        )
        assert len(requests) == 1 and tools.calls == [] and wire.closed
        assert not any(
            isinstance(item.content, ToolCallContent | ToolCallRejectionContent | ToolResultContent)
            for item in turn.items
        )
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    events = await reopened.events(thread.thread_id)
    assert restored.turns[-1] == turn and replay(events) == restored
    assert IDENTITY_CANARY not in repr(events) and IDENTITY_CANARY not in caplog.text


async def test_success_keeps_original_attempt_and_completion_contract():
    events, wire = await collect(text_frames())
    ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
    assert len(ends) == 1 and ends[0].outcome == "completed" and ends[0].error is None
    assert isinstance(events[-1], ResponseCompleted) and wire.closed


async def test_max_output_reason_does_not_release_partial_tools_or_get_failure_diagnostic():
    parts = malformed_frames("missing_id")
    parts[1] = frame(chunk(finish="length"))
    events, wire = await collect(parts)
    assert events[-1] == ResponseCompleted(
        finish_reason="max_output_tokens", usage=Usage(input_tokens=10, output_tokens=2)
    )
    assert not any(isinstance(e, ToolCallCompleted | ToolCallRejected) for e in events)
    assert [e.outcome for e in events if isinstance(e, ModelAttemptFinished)] == ["completed"]
    assert wire.closed


@pytest.mark.parametrize("reason", [IDENTITY_CANARY, "tool_name_unknown", None, 123])
def test_protocol_error_constructor_rejects_free_text_without_echo(reason):
    with pytest.raises(ValueError) as error:
        ChatProtocolError(reason)
    assert str(error.value) == "Chat协议诊断原因类型无效"
    assert IDENTITY_CANARY not in str(error.value)


@pytest.mark.parametrize("wrapped", [False, True])
def test_only_typed_diagnostic_refines_existing_failure_without_changing_fields(wrapped):
    original = finish_attempt(uuid4(), ResponseFailed(code="invalid_provider_output"))
    error = ChatProtocolError(ChatProtocolReason.TOOL_NAME_UNKNOWN)
    if wrapped:
        wrapper = APIConnectionError(request=httpx.Request("POST", "https://fixture.invalid"))
        wrapper.__cause__ = error
        error = wrapper
    changed = diagnostic_failure(error, original)
    assert changed.attempt_id == original.attempt_id and changed.outcome == original.outcome
    assert changed.error.model_dump(exclude={"message"}) == original.error.model_dump(
        exclude={"message"}
    )
    assert changed.error.message.endswith("chat_protocol/v1:tool_name_unknown")
    assert original.error.message == "Provider 返回结构化失败"


@pytest.mark.parametrize("kind", ["unknown", "subclass", "mutated", "wrong_code", "completed"])
def test_untrusted_or_inapplicable_diagnostic_returns_original_event(kind):
    original = finish_attempt(uuid4(), ResponseFailed(code="invalid_provider_output"))
    error = ChatProtocolError(ChatProtocolReason.TOOL_NAME_UNKNOWN)
    if kind == "unknown":
        error = ValueError(IDENTITY_CANARY)
    elif kind == "subclass":

        class ForgedError(ChatProtocolError):
            pass

        error = ForgedError(ChatProtocolReason.TOOL_NAME_UNKNOWN)
    elif kind == "mutated":
        error._reason = IDENTITY_CANARY
    elif kind == "wrong_code":
        original = finish_attempt(uuid4(), ResponseFailed(code="transport", retryable=True))
    else:
        original = finish_attempt(uuid4())
    changed = diagnostic_failure(error, original)
    assert changed is original and IDENTITY_CANARY not in changed.model_dump_json()


async def test_early_feed_identity_drift_records_reason_without_identity_value():
    parts = tool_frames()
    parts[1] = frame(chunk(finish="tool_calls", response_id=IDENTITY_CANARY))
    events, wire = await collect(parts)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    failure = next(e for e in events if isinstance(e, ModelAttemptFinished))
    assert failure.error.message == (
        "Provider 返回结构化失败；chat_protocol/v1:response_identity_changed"
    )
    assert wire.closed and IDENTITY_CANARY not in repr(events)
