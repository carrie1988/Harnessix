from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from pathlib import Path
from typing import Any

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.models import ApprovalRequestContent, Budget, TurnStatus, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.contracts.provider import assert_failed_attempts, model_request
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames, tool_frames

CANARY = "fixture-SECRET-CANARY"


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESSIX_TEST_KEY", CANARY)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)


def config(**updates: Any) -> OpenAIChatConfig:
    return OpenAIChatConfig(
        model="test-model",
        api_key_env="HARNESSIX_TEST_KEY",
        retry_delay_seconds=0,
        **updates,
    )


async def collect(parts: list[bytes], **limits: Any) -> tuple[list[Any], WireStream]:
    wire = WireStream(parts)
    async with OpenAIChatProvider(
        config(**limits), transport=httpx.MockTransport(lambda _: response(wire))
    ) as provider:
        events = [
            event async for event in provider.stream(model_request(with_tools=True), CancelToken())
        ]
    return events, wire


@pytest.mark.parametrize(
    "arguments",
    ["[]", "null", '"text"', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '{"a":Infinity}'],
)
async def test_invalid_arguments_never_release_calls(arguments: str) -> None:
    events, wire = await collect(tool_frames(arguments))
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(
        isinstance(e, ToolCallCompleted | ToolCallRejected | ResponseCompleted) for e in events
    )
    assert wire.closed


@pytest.mark.parametrize(
    "scenario",
    [
        "no_usage",
        "wrong_total",
        "negative_usage",
        "boolean_usage",
        "no_finish",
        "early_usage",
        "id_drift",
        "multiple_choices",
        "no_choices",
        "bad_json",
        "wrong_type",
        "unknown_tool",
        "duplicate_call_id",
        "call_id_drift",
        "name_drift",
        "missing_call_type",
        "index_gap",
        "negative_index",
        "legacy_function_call",
        "no_semantic_output",
        "content_type",
        "done_suffix",
        "duplicate_done",
    ],
)
async def test_malformed_wire(scenario: str) -> None:
    parts = tool_frames()
    if scenario == "no_usage":
        parts.pop(2)
    elif scenario in {"wrong_total", "negative_usage", "boolean_usage"}:
        usage = chunk(usage=True)
        usage["usage"][
            {
                "wrong_total": "total_tokens",
                "negative_usage": "prompt_tokens",
                "boolean_usage": "prompt_tokens",
            }[scenario]
        ] = True if scenario == "boolean_usage" else -1
        parts[2] = frame(usage)
    elif scenario == "no_finish":
        parts.pop(1)
    elif scenario == "early_usage":
        parts[1], parts[2] = parts[2], parts[1]
    elif scenario == "id_drift":
        parts[1] = frame(chunk(finish="tool_calls", response_id="other"))
    elif scenario == "multiple_choices":
        value = chunk({"tool_calls": [call()]})
        value["choices"].append({"index": 1, "delta": {}, "finish_reason": None})
        parts[0] = frame(value)
    elif scenario == "no_choices":
        value = chunk()
        value["choices"] = []
        parts[0] = frame(value)
    elif scenario == "bad_json":
        parts[0] = b"data: {bad\n\n"
    elif scenario == "wrong_type":
        parts[0] = frame(chunk({"content": 123}))
    elif scenario == "unknown_tool":
        value = call()
        value["function"]["name"] = "not-registered"
        parts[0] = frame(chunk({"tool_calls": [value]}))
    elif scenario == "duplicate_call_id":
        value = call(1)
        value["id"] = call()["id"]
        parts[0] = frame(chunk({"tool_calls": [call(), value]}))
    elif scenario == "call_id_drift":
        parts.insert(1, frame(chunk({"tool_calls": [{"index": 0, "id": "changed"}]})))
    elif scenario == "name_drift":
        parts.insert(
            1, frame(chunk({"tool_calls": [{"index": 0, "function": {"name": "changed"}}]}))
        )
    elif scenario == "missing_call_type":
        value = call()
        value.pop("type")
        parts[0] = frame(chunk({"tool_calls": [value]}))
    elif scenario in {"index_gap", "negative_index"}:
        parts[0] = frame(chunk({"tool_calls": [call(1 if scenario == "index_gap" else -1)]}))
    elif scenario == "legacy_function_call":
        parts[0] = frame(chunk({"function_call": {"name": "read", "arguments": "{}"}}))
    elif scenario == "no_semantic_output":
        parts = [frame(chunk(finish="stop")), frame(chunk(usage=True)), b"data: [DONE]\n\n"]
    elif scenario == "content_type":
        parts = [frame({"id": "bad"})]
    elif scenario == "done_suffix":
        parts[-1] = b"data: [DONE]suffix\n\n"
    elif scenario == "duplicate_done":
        parts[-1] += parts[-1]
    events, wire = await collect(parts)
    if scenario == "unknown_tool":
        assert [e for e in events if isinstance(e, ToolCallCompleted | ToolCallRejected)] == [
            ToolCallRejected(call_id=call()["id"], argument_chars=2)
        ]
        assert events[-1] == ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=2)
        )
        assert "not-registered" not in repr(events)
    else:
        assert events[-1] == ResponseFailed(code="invalid_provider_output")
        assert not any(
            isinstance(e, ToolCallCompleted | ToolCallRejected | ResponseCompleted) for e in events
        )
    assert wire.closed


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
async def test_sse_utf8_fragmentation_and_newlines(newline: bytes) -> None:
    data = b"".join(text_frames()).replace(b"\n", newline)
    events, wire = await collect([data[i : i + 1] for i in range(len(data))])
    assert isinstance(events[-1], ResponseCompleted)
    assert wire.closed


@pytest.mark.parametrize(
    ("parts", "limits"),
    [
        ([b": " + b"a" * 1024], {"max_frame_bytes": 128}),
        ([b": ping\n\n"] * 200, {"max_response_bytes": 1024}),
        ([b": ping\n\n"] * 3, {"max_chunks": 2}),
        ([b"data: " + b" " * 1024], {"max_frame_bytes": 128}),
    ],
)
async def test_wire_budgets(parts: list[bytes], limits: dict[str, int]) -> None:
    events, wire = await collect(parts, **limits)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert wire.closed


@pytest.mark.parametrize(
    ("status", "code", "expected", "attempts"),
    [
        (403, "x", "authentication", 1),
        (429, "insufficient_quota", "quota", 1),
        (400, "content_policy_violation", "content_policy", 1),
        (503, "x", "provider_internal", 2),
        (408, "x", "transport", 2),
        (400, "x", "invalid_request", 1),
        (302, "x", "invalid_request", 1),
    ],
)
async def test_status_mapping_and_no_sdk_retries(
    status: int, code: str, expected: str, attempts: int, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://other.invalid"},
            json={"error": {"code": code, "message": CANARY}},
        )

    with caplog.at_level(logging.INFO):
        async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
            events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert_failed_attempts(events, ResponseFailed(code=expected, retryable=attempts > 1), attempts)
    assert len(requests) == attempts
    assert CANARY not in repr(events) + caplog.text


async def test_retry_then_success_and_credential_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_ADMIN_KEY"]:
        monkeypatch.setenv(name, "foreign-canary")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.invalid")
    requests: list[httpx.Request] = []
    wire = WireStream(text_frames())

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError(CANARY)
        return response(wire)

    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        request = model_request().model_copy(update={"remaining_tokens": 20})
        events = [e async for e in provider.stream(request, CancelToken())]
        assert CANARY not in repr(provider) + repr(provider.config) + request.model_dump_json()
    assert isinstance(events[-1], ResponseCompleted)
    assert len(requests) == 2
    for outgoing in requests:
        assert str(outgoing.url) == "https://api.openai.com/v1/chat/completions"
        assert outgoing.headers["authorization"] == "Bearer " + CANARY
        assert outgoing.headers["accept-encoding"] == "identity"
        assert "foreign-canary" not in str(outgoing.headers)
        body = json.loads(outgoing.content)
        assert body["max_completion_tokens"] == 20
        assert "tools" not in body and "parallel_tool_calls" not in body
        assert CANARY not in outgoing.content.decode()


async def test_explicit_thinking_mode_reaches_openai_compatible_wire() -> None:
    requests: list[dict[str, Any]] = []
    wire = WireStream(text_frames())

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return response(wire)

    async with OpenAIChatProvider(
        config(enable_thinking=False), transport=httpx.MockTransport(handle)
    ) as provider:
        events = [event async for event in provider.stream(model_request(), CancelToken())]

    assert isinstance(events[-1], ResponseCompleted)
    assert requests[0]["enable_thinking"] is False
    assert "extra_body" not in requests[0]


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_bounded_thinking_accounts_full_usage_without_publishing_reasoning(
    tmp_path: Path, finish: str
) -> None:
    usage = chunk(usage=True)
    usage["usage"] = {
        "prompt_tokens": 10,
        "completion_tokens": 1002,
        "total_tokens": 1012,
        "completion_tokens_details": {"reasoning_tokens": 1000, "text_tokens": 1002},
    }
    private_reasoning = "PRIVATE-REASONING-CANARY"
    wire = WireStream(
        [
            frame(chunk({"role": "assistant", "reasoning_content": private_reasoning})),
            frame(chunk({"content": "OK"})),
            frame(chunk(finish=finish)),
            frame(usage),
            b"data: [DONE]\n\n",
        ]
    )
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return response(wire)

    store = SQLiteSessionStore(tmp_path / "thinking.db")
    tools = RecordingTools()
    async with OpenAIChatProvider(
        config(enable_thinking=True, thinking_budget=1024, max_output_tokens=4096, max_attempts=1),
        transport=httpx.MockTransport(handle),
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "任务", request_id="bounded-thinking")

    assert len(sent) == 1
    assert sent[0]["enable_thinking"] is True and sent[0]["thinking_budget"] == 1024
    assert sent[0]["max_completion_tokens"] == 4096
    assert "max_tokens" not in sent[0] and "extra_body" not in sent[0]
    assert turn.status == (TurnStatus.COMPLETED if finish == "stop" else TurnStatus.FAILED)
    assert turn.usage == Usage(input_tokens=10, output_tokens=1002)
    assert turn.model_attempts[0].usage.reasoning_output_tokens == 1000
    assert turn.usage_is_complete and tools.calls == [] and wire.closed
    events = await store.events(thread.thread_id)
    assert private_reasoning not in "".join(event.model_dump_json() for event in events)
    assert replay(events) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("total,executed_calls", [(49999, 1), (50000, 0), (50010, 0)])
async def test_thinking_tool_proposal_keeps_full_usage_before_hard_budget_dispatch(
    tmp_path: Path, total: int, executed_calls: int
) -> None:
    usage = chunk(usage=True)
    usage["usage"] = {
        "prompt_tokens": total - 4106,
        "completion_tokens": 4106,
        "total_tokens": total,
        "completion_tokens_details": {"reasoning_tokens": 1000},
    }
    parts = tool_frames()
    parts[2] = frame(usage)
    wire = WireStream(parts)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(wire)

    tools = RecordingTools()
    store = SQLiteSessionStore(tmp_path / "tool-budget.db")
    async with OpenAIChatProvider(
        config(enable_thinking=True, thinking_budget=1024, max_output_tokens=4096, max_attempts=1),
        transport=httpx.MockTransport(handle),
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id,
                "任务",
                request_id="thinking-tool-budget",
                budget=Budget(max_tokens=50000),
            )

    assert len(requests) == 1 and wire.closed
    assert turn.status == TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.usage.total_tokens == total and turn.usage.output_tokens == 4106
    assert turn.budget.max_tokens == 50000 and len(tools.calls) == executed_calls
    assert turn.model_attempts[0].usage.reasoning_output_tokens == 1000
    assert turn.usage_is_complete
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("available", [True, False])
async def test_model_availability_probe_is_read_only_and_exact(available: bool) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        models = ["test-model"] if available else ["test-model-newer"]
        return httpx.Response(
            200,
            request=request,
            json={
                "object": "list",
                "data": [
                    {"id": model, "object": "model", "created": 0, "owned_by": "provider"}
                    for model in models
                ],
            },
        )

    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        result = await provider.probe_model_availability(CancelToken())

    assert result is available
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"
    assert requests[0].content == b""


async def test_model_availability_probe_honors_pre_cancel() -> None:
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        pytest.fail("取消后的目录预检不应发网")

    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        cancel = CancelToken()
        cancel.cancel()
        with pytest.raises(TurnCancelled):
            await provider.probe_model_availability(cancel)

    assert requests == 0


async def test_model_availability_probe_obeys_io_timeout() -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with OpenAIChatProvider(
        config(io_timeout_seconds=0.01), transport=httpx.MockTransport(handle)
    ) as provider:
        with pytest.raises(TimeoutError):
            await provider.probe_model_availability(CancelToken())


@pytest.mark.parametrize("where", ["connect", "backoff", "body", "parent_task", "before_start"])
async def test_cancel_each_wait(where: str) -> None:
    entered = asyncio.Event()
    wire = WireStream([], block=True)
    requests = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if where == "connect":
            entered.set()
            await asyncio.Event().wait()
        if where == "backoff":
            entered.set()
            return httpx.Response(429, json={"error": {"code": "rate_limit"}})
        return response(wire)

    configuration = config().model_copy(update={"retry_delay_seconds": 10})
    async with OpenAIChatProvider(configuration, transport=httpx.MockTransport(handle)) as provider:
        token = CancelToken()
        if where == "before_start":
            token.cancel()

        async def consume() -> None:
            async with aclosing(provider.stream(model_request(), token)) as stream:
                async for _ in stream:
                    pass

        task = asyncio.create_task(consume())
        if where != "before_start":
            await asyncio.wait_for(
                (entered if where in {"connect", "backoff"} else wire.entered).wait(), 2
            )
            if where == "parent_task":
                task.cancel()
            else:
                token.cancel()
        with pytest.raises(asyncio.CancelledError if where == "parent_task" else TurnCancelled):
            await asyncio.wait_for(task, 2)
        if where in {"body", "parent_task"}:
            assert wire.closed
        assert requests == (0 if where == "before_start" else 1)


async def test_total_timeout_survives_new_task_per_anext() -> None:
    wire = WireStream(text_frames()[:1], block=True)
    async with OpenAIChatProvider(
        config(timeout_seconds=0.05), transport=httpx.MockTransport(lambda _: response(wire))
    ) as provider:
        events = []
        token = CancelToken()
        async with aclosing(provider.stream(model_request(), token)) as stream:
            while True:
                try:
                    events.append(await asyncio.wait_for(token.run(anext(stream)), 2))
                except StopAsyncIteration:
                    break
    assert events[-1] == ResponseFailed(code="transport", retryable=True)
    assert wire.closed


@pytest.mark.parametrize("encoding", ["gzip", "br"])
async def test_compressed_response_rejected(encoding: str) -> None:
    wire = WireStream(text_frames())

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=wire)

    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert_failed_attempts(events, ResponseFailed(code="invalid_provider_output"))
    assert wire.closed


async def test_kernel_tool_loop_pairing_usage_and_replay(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    wires: list[WireStream] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        wire = WireStream(tool_frames() if len(requests) < 3 else text_frames())
        wires.append(wire)
        return response(wire)

    store = SQLiteSessionStore(tmp_path / "session.db")
    tools = RecordingTools()
    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id,
                "读取测试值",
                request_id="r",
                budget=Budget(max_tokens=50_000, max_output_chars=1_024),
            )
    assert turn.status == TurnStatus.COMPLETED
    assert turn.usage == Usage(input_tokens=30, output_tokens=6)
    assert turn.usage_is_complete
    assert [(a.step, a.index, a.status) for a in turn.model_attempts] == [
        (1, 1, "completed"),
        (2, 1, "completed"),
        (3, 1, "completed"),
    ]
    assert len(tools.calls) == 2
    assert tools.calls[0].provider_call_id == tools.calls[1].provider_call_id
    messages = requests[-1]["messages"]
    calls = [m["tool_calls"][0]["id"] for m in messages if "tool_calls" in m]
    results = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert len(set(calls)) == 2 and calls == results
    assert [r["max_completion_tokens"] for r in requests] == [1_024, 1_024, 1_024]
    assert all(w.closed for w in wires)
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)
    assert CANARY not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("finish", ["length", "content_filter"])
async def test_failed_finish_records_known_usage_without_executing_tools(
    tmp_path: Path, finish: str
) -> None:
    parts = tool_frames("{")
    parts[1] = frame(chunk(finish=finish))
    wire = WireStream(parts)
    tools = RecordingTools()
    async with OpenAIChatProvider(
        config(), transport=httpx.MockTransport(lambda _: response(wire))
    ) as provider:
        async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert turn.status == TurnStatus.FAILED
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)
    assert tools.calls == []


async def test_text_only_capability_rejects_tools_without_request() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        pytest.fail("不支持的请求不应发网")

    async with OpenAIChatProvider(
        config(capabilities=ChatCapabilities(tool_calls=False, parallel_tool_calls=False)),
        transport=httpx.MockTransport(handle),
    ) as provider:
        events = [e async for e in provider.stream(model_request(with_tools=True), CancelToken())]
    assert events == [ResponseFailed(code="invalid_request")]


@pytest.mark.parametrize("variable", ["missing", "invalid", "headers"])
def test_configuration_fails_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    if variable == "missing":
        monkeypatch.delenv("HARNESSIX_TEST_KEY")
    elif variable == "invalid":
        monkeypatch.setenv("HARNESSIX_TEST_KEY", CANARY + "\n")
    else:
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Secret: " + CANARY)
    with pytest.raises(ValueError) as failure:
        OpenAIChatProvider(config())
    assert CANARY not in str(failure.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://a:b@example.com/v1",
        "https://example.com/?key=x",
        "https://example.com/#key",
        "https://",
        "https://exa mple.com",
    ],
)
def test_endpoint_constraints(url: str) -> None:
    with pytest.raises(ValueError):
        config(base_url=url)


@pytest.mark.parametrize("cancel_body", [False, True])
async def test_error_response_body_is_bounded_and_closed(cancel_body: bool) -> None:
    wire = WireStream([] if cancel_body else [b"x" * 2048], block=cancel_body)

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=wire)

    async with OpenAIChatProvider(
        config(max_response_bytes=1024), transport=httpx.MockTransport(handle)
    ) as provider:
        token = CancelToken()

        async def consume() -> list[Any]:
            return [e async for e in provider.stream(model_request(), token)]

        if cancel_body:
            task = asyncio.create_task(consume())
            await asyncio.wait_for(wire.entered.wait(), 2)
            token.cancel()
            with pytest.raises(TurnCancelled):
                await task
        else:
            assert_failed_attempts(await consume(), ResponseFailed(code="invalid_provider_output"))
        assert wire.closed


@pytest.mark.parametrize("limit", ["text", "calls", "parallel"])
async def test_semantic_output_limits(limit: str) -> None:
    wire = WireStream(
        text_frames()
        if limit == "text"
        else [
            frame(chunk({"tool_calls": [call(0), call(1)]})),
            *tool_frames()[1:],
        ]
    )
    budget = Budget(max_output_chars=1) if limit == "text" else Budget(max_tool_calls_per_step=1)
    if limit == "parallel":
        budget = Budget()
    async with OpenAIChatProvider(
        config(capabilities=ChatCapabilities(parallel_tool_calls=limit != "parallel")),
        transport=httpx.MockTransport(lambda _: response(wire)),
    ) as provider:
        request = model_request(with_tools=True).model_copy(update={"budget": budget})
        events = [e async for e in provider.stream(request, CancelToken())]
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)
    assert wire.closed


@pytest.mark.parametrize("kind", ["refusal", "sse_error", "not_sse"])
async def test_other_protocol_errors(kind: str) -> None:
    parts = [frame(chunk({"refusal": "不应传播 " + CANARY}))]
    if kind == "sse_error":
        parts = [frame({"error": {"code": "insufficient_quota", "message": CANARY}})]
    wire = WireStream(parts)

    def handle(_: httpx.Request) -> httpx.Response:
        outgoing = response(wire)
        if kind == "not_sse":
            outgoing.headers["content-type"] = "application/json"
        return outgoing

    async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
        events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert_failed_attempts(
        events,
        ResponseFailed(
            code={
                "refusal": "content_policy",
                "sse_error": "quota",
                "not_sse": "invalid_provider_output",
            }[kind]
        ),
    )
    assert CANARY not in repr(events) and wire.closed


async def test_approval_restart_with_real_adapter(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "approval.db")
    tools = RecordingTools(approval=True)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(WireStream(tool_frames() if len(requests) == 1 else text_frames()))

    for restart in (False, True):
        async with OpenAIChatProvider(config(), transport=httpx.MockTransport(handle)) as provider:
            async with AgentRuntime(store, provider, tools) as runtime:
                if not restart:
                    thread = await runtime.create_thread(str(tmp_path))
                    turn = await runtime.run_turn(thread.thread_id, "读取", request_id="r")
                    assert turn.status == TurnStatus.WAITING_APPROVAL
                    assert tools.calls == [] and len(requests) == 1
                else:
                    approval = next(
                        i.content
                        for i in turn.items
                        if isinstance(i.content, ApprovalRequestContent)
                    )
                    await runtime.reply_approval(
                        thread.thread_id,
                        turn.turn_id,
                        approval.approval_id,
                        fingerprint=approval.request_fingerprint,
                        decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="测试"),
                    )
                    assert tools.calls == [] and len(requests) == 1
                    resumed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                    assert resumed.status == TurnStatus.COMPLETED
                    assert len(tools.calls) == 1 and len(requests) == 2
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_concurrent_requests_have_independent_stream_state() -> None:
    async with OpenAIChatProvider(
        config(), transport=httpx.MockTransport(lambda _: response(WireStream(text_frames())))
    ) as provider:

        async def consume() -> list[Any]:
            return [e async for e in provider.stream(model_request(), CancelToken())]

        results = await asyncio.gather(*(consume() for _ in range(5)))
        assert all(isinstance(events[-1], ResponseCompleted) for events in results)
    assert [e async for e in provider.stream(model_request(), CancelToken())] == [
        ResponseFailed(code="invalid_request")
    ]
