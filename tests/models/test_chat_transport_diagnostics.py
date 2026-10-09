"""传输诊断只投影固定类型/状态；使用正式SDK、离线HTTP和原持久状态机。"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models import _provider_io
from harnessix.models._bounded_http import InvalidWireData
from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason, diagnostic_failure
from harnessix.models._provider_io import finish_attempt
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    TextDelta,
    ToolCallCompleted,
)
from harnessix.models.openai_chat import OpenAIChatProvider, _failed_response
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import MODEL, GuardedVerificationProvider
from tests.agent.helpers import RecordingTools
from tests.contracts.provider import assert_failed_attempts, model_request
from tests.deadlines import capture_deadlines
from tests.evals.test_provider_verification_budget import bounds
from tests.models.test_openai_chat import config
from tests.models.wire import WireStream, response, text_frames

CANARY = "TRANSPORT_PRIVATE_CANARY_NOT_FOR_DIAGNOSTICS"
TRANSPORT_CASES = [
    (httpx.ConnectTimeout, "httpx_connect_timeout"),
    (httpx.ReadTimeout, "httpx_read_timeout"),
    (httpx.WriteTimeout, "httpx_write_timeout"),
    (httpx.PoolTimeout, "httpx_pool_timeout"),
    (httpx.ConnectError, "httpx_connect_failure"),
    (httpx.ReadError, "httpx_read_failure"),
    (httpx.WriteError, "httpx_write_failure"),
    (httpx.ProxyError, "httpx_proxy_failure"),
    (httpx.ProtocolError, "httpx_protocol_failure"),
    (httpx.LocalProtocolError, "httpx_protocol_failure"),
    (httpx.RemoteProtocolError, "httpx_protocol_failure"),
]


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(
        "harnessix.models.openai_chat.read_key", lambda *a, **kw: pytest.fail("不得读取Key")
    )


def private_request():
    return httpx.Request(
        "POST",
        "https://fixture.invalid/" + CANARY,
        headers={"authorization": CANARY},
        content=CANARY,
    )


def assert_diagnostic(events, reason):
    ends = [e for e in events if isinstance(e, ModelAttemptFinished)]
    starts = [e for e in events if isinstance(e, ModelAttemptStarted)]
    assert len(ends) == len(starts) == 1
    end = ends[0]
    original = finish_attempt(starts[0].attempt_id, events[-1])
    assert end.error.message == f"Provider 返回结构化失败；chat_transport/v1:{reason}"
    assert end.model_copy(update={"error": original.error}) == original
    assert end.error.model_dump(exclude={"message"}) == original.error.model_dump(
        exclude={"message"}
    )
    assert CANARY not in repr(events)


@pytest.mark.parametrize("error_type,reason", TRANSPORT_CASES)
async def test_sdk_before_first_packet_distinguishes_transport_and_never_retries(
    error_type, reason, caplog
):
    requests = 0

    def handle(_):
        nonlocal requests
        requests += 1
        raise error_type(CANARY, request=private_request())

    with caplog.at_level(logging.INFO):
        async with OpenAIChatProvider(
            config(max_attempts=1), api_key=CANARY, transport=httpx.MockTransport(handle)
        ) as provider:
            events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert requests == 1
    assert_failed_attempts(events, ResponseFailed(code="transport", retryable=True))
    assert_diagnostic(events, reason)
    assert CANARY not in caplog.text


@pytest.mark.parametrize("error_type,reason", TRANSPORT_CASES)
@pytest.mark.parametrize("wrapped", [False, True])
def test_direct_or_single_sdk_cause_preserves_all_nonmessage_fields(error_type, reason, wrapped):
    error = error_type(CANARY, request=private_request())
    if wrapped:
        wrapper = APIConnectionError(message=CANARY, request=private_request())
        wrapper.__cause__ = error
        error = wrapper
    failure, changed = _failed_response(uuid4(), error)
    original = finish_attempt(changed.attempt_id, failure)
    assert failure == ResponseFailed(code="transport", retryable=True)
    assert changed.model_copy(update={"error": original.error}) == original
    assert changed.error.model_copy(update={"message": original.error.message}) == original.error
    assert changed.error.message.endswith("chat_transport/v1:" + reason)
    assert CANARY not in changed.model_dump_json()


@pytest.mark.parametrize(
    "status,code,reason",
    [
        (408, "transport", "http_status_408"),
        (409, "transport", "http_status_409"),
        (429, "rate_limit", None),
    ],
)
async def test_sdk_verified_status_does_not_relabel_429(status, code, reason, caplog):
    requests = 0

    def handle(_):
        nonlocal requests
        requests += 1
        return httpx.Response(
            status, headers={"x-private-canary": CANARY}, json={"error": {"message": CANARY}}
        )

    with caplog.at_level(logging.INFO):
        async with OpenAIChatProvider(
            config(max_attempts=1), api_key=CANARY, transport=httpx.MockTransport(handle)
        ) as provider:
            events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert requests == 1
    assert_failed_attempts(events, ResponseFailed(code=code, retryable=True))
    if reason is not None:
        assert_diagnostic(events, reason)
    else:
        assert events[-2].error.message == "Provider 返回结构化失败"
    assert CANARY not in repr(events) + caplog.text


@pytest.mark.parametrize("native_timeout", [False, True])
async def test_stream_failure_preserves_exposed_text_and_last_usage_without_retry(native_timeout):
    class NativeTimeoutStream(WireStream):
        async def __aiter__(self):
            async for part in super().__aiter__():
                yield part
            raise TimeoutError(CANARY)

    wire = (
        NativeTimeoutStream(text_frames()[:-1])
        if native_timeout
        else WireStream(text_frames()[:-1], fail=True)
    )
    requests = 0

    def handle(_):
        nonlocal requests
        requests += 1
        return response(wire)

    async with OpenAIChatProvider(
        config(max_attempts=3), api_key=CANARY, transport=httpx.MockTransport(handle)
    ) as provider:
        events = [e async for e in provider.stream(model_request(with_tools=True), CancelToken())]
    assert requests == 1 and wire.closed
    assert events[-1] == ResponseFailed(code="transport", retryable=True)
    assert_diagnostic(events, "timeout_origin_unknown" if native_timeout else "httpx_read_failure")
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "你好"
    observations = [e for e in events if isinstance(e, ModelUsageObserved)]
    assert len(observations) == 2 and observations[0].usage.completeness == "unknown"
    assert observations[-1].usage.completeness == "complete"
    assert (observations[-1].usage.input_tokens, observations[-1].usage.output_tokens) == (10, 2)
    assert all(e.attempt_id == events[0].attempt_id for e in observations)
    assert not any(isinstance(e, ResponseCompleted | ToolCallCompleted) for e in events)


async def test_real_async_deadline_still_has_unknown_origin_diagnostic(monkeypatch):
    deadlines = capture_deadlines(monkeypatch, _provider_io)
    wire = WireStream([], block=True)
    async with OpenAIChatProvider(
        config(max_attempts=1),
        api_key=CANARY,
        transport=httpx.MockTransport(lambda _: response(wire)),
    ) as provider:

        async def consume():
            return [e async for e in provider.stream(model_request(), CancelToken())]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(wire.entered.wait(), 2)
        deadlines[-1].reschedule(asyncio.get_running_loop().time())
        events = await asyncio.wait_for(task, 2)
    assert deadlines[-1].expired() and wire.closed
    assert_failed_attempts(events, ResponseFailed(code="transport", retryable=True))
    assert_diagnostic(events, "timeout_origin_unknown")


@pytest.mark.parametrize("method", ["token", "task"])
@pytest.mark.parametrize("after_usage", [False, True])
async def test_cancellation_is_not_a_transport_failure(method, after_usage):
    wire = WireStream(text_frames()[:-1] if after_usage else [], block=True)
    events = []
    cancel = CancelToken()
    async with OpenAIChatProvider(
        config(max_attempts=1),
        api_key=CANARY,
        transport=httpx.MockTransport(lambda _: response(wire)),
    ) as provider:

        async def consume():
            async for event in provider.stream(model_request(), cancel):
                events.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(wire.entered.wait(), 2)
        if method == "task":
            task.cancel()
        else:
            cancel.cancel()
        with pytest.raises(asyncio.CancelledError if method == "task" else TurnCancelled):
            await asyncio.wait_for(task, 2)
    assert wire.closed
    assert not any(isinstance(e, ModelAttemptFinished | ResponseFailed) for e in events)
    observations = [e for e in events if isinstance(e, ModelUsageObserved)]
    assert len(observations) == (2 if after_usage else 0)
    if after_usage:
        assert observations[-1].usage.completeness == "complete"
        assert (observations[-1].usage.input_tokens, observations[-1].usage.output_tokens) == (
            10,
            2,
        )


@pytest.mark.parametrize(
    "kind",
    [
        "opaque",
        "base_transport",
        "base_timeout",
        "sdk_timeout_without_cause",
        "nested",
        "cycle",
        "os_text",
        "wrapped_deadline",
        "subclass",
        "wrong_code",
        "completed",
    ],
)
def test_unknown_causes_and_inapplicable_attempts_remain_generic(kind):
    original = finish_attempt(uuid4(), ResponseFailed(code="transport", retryable=True))
    error = APIConnectionError(message=CANARY, request=private_request())
    cause = ValueError(CANARY)
    if kind == "base_transport":
        cause = httpx.TransportError(CANARY)
    elif kind == "base_timeout":
        cause = httpx.TimeoutException(CANARY)
    elif kind == "sdk_timeout_without_cause":
        error = APITimeoutError(request=private_request())
    elif kind == "nested":
        cause.__cause__ = httpx.ReadTimeout(CANARY)
    elif kind == "cycle":
        cause = error
    elif kind == "os_text":
        cause = OSError("429 connect read timeout " + CANARY)
    elif kind == "wrapped_deadline":
        cause = TimeoutError(CANARY)
    elif kind == "subclass":

        class CustomReadError(httpx.ReadError):
            pass

        cause = CustomReadError(CANARY)
    elif kind == "wrong_code":
        original = finish_attempt(uuid4(), ResponseFailed(code="rate_limit", retryable=True))
        cause = httpx.ReadTimeout(CANARY)
    elif kind == "completed":
        original = finish_attempt(uuid4())
        cause = httpx.ReadTimeout(CANARY)
    if kind != "sdk_timeout_without_cause":
        error.__cause__ = cause
    assert diagnostic_failure(error, original) is original
    assert CANARY not in original.model_dump_json()


@pytest.mark.parametrize("reported", [True, "408", 409, 429])
def test_status_diagnostic_requires_strict_bound_408_or_409(reported):
    error = APIStatusError(
        CANARY, response=httpx.Response(408, request=private_request()), body=None
    )
    error.status_code = reported
    original = finish_attempt(uuid4(), ResponseFailed(code="transport", retryable=True))
    assert diagnostic_failure(error, original) is original


@pytest.mark.parametrize("protocol", [False, True])
def test_invalid_wire_cause_retains_priority_over_connection_and_http_status(protocol):
    cause = (
        ChatProtocolError(ChatProtocolReason.TOOL_ID_MISSING)
        if protocol
        else InvalidWireData(CANARY)
    )
    cause.__cause__ = httpx.ReadTimeout(CANARY)
    for error in (
        APIConnectionError(message=CANARY, request=private_request()),
        APIStatusError(CANARY, response=httpx.Response(408, request=private_request()), body=None),
    ):
        error.__cause__ = cause
        failure, attempt = _failed_response(uuid4(), error)
        assert failure == ResponseFailed(code="invalid_provider_output")
        assert attempt.error.code == "provider_invalid_provider_output"
        expected = "Provider 返回结构化失败"
        if protocol:
            expected += "；chat_protocol/v1:tool_id_missing"
        assert attempt.error.message == expected
        assert CANARY not in attempt.model_dump_json()


@pytest.mark.parametrize("after_usage", [False, True])
async def test_native_session_reopen_and_replay_keep_diagnostic_and_usage(tmp_path, after_usage):
    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    tools = RecordingTools()
    wire = WireStream(text_frames()[:-1], fail=True)

    def handle(_):
        if not after_usage:
            raise httpx.ConnectError(CANARY, request=private_request())
        return response(wire)

    async with OpenAIChatProvider(
        config(max_attempts=1), api_key=CANARY, transport=httpx.MockTransport(handle)
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "离线诊断", request_id="r")
    assert turn.status == "failed" and turn.error.code == "provider_transport"
    assert turn.error.message == "Provider 返回结构化失败"
    assert len(turn.model_attempts) == 1 and tools.calls == []
    attempt = turn.model_attempts[0]
    reason = "httpx_read_failure" if after_usage else "httpx_connect_failure"
    assert attempt.error.message.endswith("chat_transport/v1:" + reason)
    assert attempt.usage.completeness == ("complete" if after_usage else "unknown")
    assert (attempt.usage.input_tokens, attempt.usage.output_tokens) == (
        (10, 2) if after_usage else (None, None)
    )
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    events = await reopened.events(thread.thread_id)
    assert restored.turns[-1] == turn and replay(events) == restored
    assert CANARY not in repr(events)
    assert CANARY.encode() not in store.path.read_bytes()


async def test_original_guard_stops_unknown_without_opening_a_fee_owner():
    ledger = Mock(spec=VerificationBudgetLedger)
    ledger.reserve.return_value = uuid4()
    suite_cancel = CancelToken()
    requests = 0

    def handle(_):
        nonlocal requests
        requests += 1
        raise httpx.ConnectTimeout(CANARY, request=private_request())

    async with OpenAIChatProvider(
        config(max_attempts=1).model_copy(update={"model": MODEL}),
        api_key=CANARY,
        transport=httpx.MockTransport(handle),
    ) as provider:
        guard = GuardedVerificationProvider(provider, ledger, bounds(), suite_cancel)
        request = model_request()
        events = [e async for e in guard.stream(request, CancelToken())]
        assert_failed_attempts(events, ResponseFailed(code="transport", retryable=True))
        assert_diagnostic(events, "httpx_connect_timeout")
        ledger.settle.assert_called_once_with(ledger.reserve.return_value, None, sent=True)
        assert suite_cancel.cancelled
        with pytest.raises(TurnCancelled):
            _ = [e async for e in guard.stream(request, CancelToken())]
    assert requests == ledger.reserve.call_count == 1
