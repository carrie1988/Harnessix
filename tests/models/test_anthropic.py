from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.models import ApprovalRequestContent, Budget, TurnStatus, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted, ResponseFailed, ToolCallCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.contracts.provider import model_request
from tests.models import wire as openai_wire
from tests.models.anthropic_wire import (
    WireStream,
    frame,
    response,
    start,
    stop,
    text_frames,
    tool_block,
    tool_frames,
)

CANARY = "fixture-ANTHROPIC-SECRET-CANARY"


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESSIX_TEST_ANTHROPIC_KEY", CANARY)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)


def config(**updates: Any) -> AnthropicConfig:
    return AnthropicConfig(
        model="test-model",
        api_key_env="HARNESSIX_TEST_ANTHROPIC_KEY",
        retry_delay_seconds=0,
        **updates,
    )


async def collect(parts: list[bytes], **limits: Any) -> tuple[list[Any], WireStream]:
    wire = WireStream(parts)
    async with AnthropicProvider(
        config(**limits), transport=httpx2.MockTransport(lambda _: response(wire))
    ) as provider:
        events = [e async for e in provider.stream(model_request(with_tools=True), CancelToken())]
    return events, wire


def unpack(part: bytes) -> dict[str, Any]:
    return json.loads(part.split(b"data: ", 1)[1])


def repack(value: dict[str, Any]) -> bytes:
    return frame(value["type"], **{k: v for k, v in value.items() if k != "type"})


@pytest.mark.parametrize(
    "arguments", ["{", "[]", "null", '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '{"a":Infinity}']
)
async def test_bad_tool_json_never_releases_call(arguments: str) -> None:
    events, wire = await collect(tool_frames(arguments))
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)
    assert wire.closed


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_start",
        "no_start",
        "start_content",
        "start_finish",
        "wrong_role",
        "bad_json",
        "duplicate_json_key",
        "unknown_event",
        "missing_type",
        "mismatched_type",
        "duplicate_stop",
        "tail_ping",
        "tail_tool",
        "no_stop",
        "no_final_usage",
        "no_finish",
        "missing_delta_usage",
        "unknown_finish",
        "usage_rewind",
        "usage_bool",
        "usage_negative",
        "cache_missing",
        "cache_negative",
        "block_gap",
        "duplicate_block",
        "delta_without_block",
        "delta_after_stop",
        "stop_without_block",
        "unclosed_block",
        "duplicate_block_stop",
        "wrong_delta",
        "unknown_tool",
        "duplicate_tool_id",
        "initial_tool_input",
        "thinking",
        "server_tool",
        "text_tool_finish",
        "tool_text_finish",
        "empty",
        "server_usage",
    ],
)
async def test_invalid_protocol(case: str) -> None:
    parts = tool_frames()
    if case == "duplicate_start":
        parts.insert(1, start())
    elif case == "no_start":
        parts.pop(0)
    elif case in {"start_content", "start_finish", "wrong_role"}:
        value = unpack(parts[0])
        key, new = {
            "start_content": ("content", [{"type": "text", "text": "bad"}]),
            "start_finish": ("stop_reason", "end_turn"),
            "wrong_role": ("role", "user"),
        }[case]
        value["message"][key] = new
        parts[0] = repack(value)
    elif case == "bad_json":
        parts[0] = b"event: message_start\ndata: {bad\n\n"
    elif case == "duplicate_json_key":
        parts[0] = b'event: ping\ndata: {"type":"ping","type":"ping"}\n\n'
    elif case == "unknown_event":
        parts.insert(1, frame("new_semantic_event"))
    elif case == "missing_type":
        parts[0] = b"event: message_start\ndata: {}\n\n"
    elif case == "mismatched_type":
        parts[0] = b'event: message_start\ndata: {"type":"ping"}\n\n'
    elif case == "duplicate_stop":
        parts.append(frame("message_stop"))
    elif case == "tail_ping":
        parts.append(frame("ping"))
    elif case == "tail_tool":
        parts.extend(tool_block(1))
    elif case == "no_stop":
        parts.pop()
    elif case == "no_final_usage":
        parts.pop(-2)
    elif case in {"no_finish", "missing_delta_usage", "unknown_finish"}:
        value = unpack(parts[-2])
        if case == "missing_delta_usage":
            value.pop("usage")
        else:
            value["delta"]["stop_reason"] = None if case == "no_finish" else "unknown"
        parts[-2] = repack(value)
    elif case in {"usage_rewind", "usage_bool", "usage_negative"}:
        value = {"usage_rewind": 0, "usage_bool": True, "usage_negative": -1}[case]
        parts[-2] = stop("tool_use", output_tokens=value)[0]
    elif case in {"cache_missing", "cache_negative"}:
        parts[0] = start(cache_read_input_tokens=None if case == "cache_missing" else -1)
    elif case == "block_gap":
        parts[1] = tool_block(1)[0]
    elif case == "duplicate_block":
        parts.insert(2, parts[1])
    elif case == "delta_without_block":
        parts.pop(1)
    elif case == "delta_after_stop":
        parts[2], parts[3] = parts[3], parts[2]
    elif case == "stop_without_block":
        parts = [start(), frame("content_block_stop", index=0), *stop()]
    elif case == "unclosed_block":
        parts.pop(3)
    elif case == "duplicate_block_stop":
        parts.insert(4, parts[3])
    elif case == "wrong_delta":
        parts[2] = frame(
            "content_block_delta", index=0, delta={"type": "text_delta", "text": "bad"}
        )
    elif case == "unknown_tool":
        value = unpack(parts[1])
        value["content_block"]["name"] = "unknown"
        parts[1] = repack(value)
    elif case == "duplicate_tool_id":
        more = tool_block(1)
        value = unpack(more[0])
        value["content_block"]["id"] = "toolu_0"
        more[0] = repack(value)
        parts[-2:-2] = more
    elif case == "initial_tool_input":
        value = unpack(parts[1])
        value["content_block"]["input"] = {"x": 1}
        parts[1] = repack(value)
    elif case == "thinking":
        parts[1] = frame(
            "content_block_start",
            index=0,
            content_block={"type": "thinking", "thinking": CANARY, "signature": CANARY},
        )
    elif case == "server_tool":
        parts[1] = frame(
            "content_block_start",
            index=0,
            content_block={"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}},
        )
    elif case == "text_tool_finish":
        parts = text_frames()[:-2] + stop("tool_use")
    elif case == "tool_text_finish":
        parts[-2] = stop()[0]
    elif case == "empty":
        parts = [start(), *stop()]
    elif case == "server_usage":
        parts[0] = start(server_tool_use={"web_search_requests": 1, "web_fetch_requests": 0})
    events, wire = await collect(parts)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)
    assert CANARY not in repr(events) and wire.closed


async def test_cumulative_usage_includes_caches_without_double_counting() -> None:
    parts = text_frames()
    parts[0] = start(cache_read_input_tokens=20, cache_creation_input_tokens=5)
    parts.insert(
        -2, frame("message_delta", delta={"stop_reason": None}, usage={"output_tokens": 2})
    )
    parts[-2] = stop(output_tokens=4, input_tokens=12, cache_read_input_tokens=21)[0]
    events, _ = await collect(parts)
    assert events[-1] == ResponseCompleted(usage=Usage(input_tokens=38, output_tokens=4))


async def test_cache_counts_can_arrive_in_final_delta() -> None:
    parts = text_frames()
    parts[0] = start(cache_read_input_tokens=None, cache_creation_input_tokens=None)
    parts[-2] = stop(cache_read_input_tokens=2, cache_creation_input_tokens=3)[0]
    events, _ = await collect(parts)
    assert events[-1].usage == Usage(input_tokens=15, output_tokens=2)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
async def test_ping_and_arbitrary_utf8_fragmentation(newline: bytes) -> None:
    parts = text_frames()
    parts.insert(1, frame("ping"))
    parts.insert(2, b": heartbeat\n\n")
    data = b"".join(parts).replace(b"\n", newline)
    events, wire = await collect([data[i : i + 1] for i in range(len(data))])
    assert isinstance(events[-1], ResponseCompleted) and wire.closed


@pytest.mark.parametrize(
    ("status", "kind", "expected", "attempts"),
    [
        (403, "permission_error", "authentication", 1),
        (402, "billing_error", "quota", 1),
        (400, "invalid_request_error", "invalid_request", 1),
        (413, "request_too_large", "invalid_request", 1),
        (529, "overloaded_error", "provider_internal", 2),
        (500, "api_error", "provider_internal", 2),
        (504, "timeout_error", "transport", 2),
        (302, "x", "invalid_request", 1),
        (200, "overloaded_error", "provider_internal", 2),
        (200, "billing_error", "quota", 1),
    ],
)
async def test_http_and_sse_errors_are_bounded_and_redacted(
    status: int, kind: str, expected: str, attempts: int, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[httpx2.Request] = []
    wires: list[WireStream] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if status == 200:
            wire = WireStream([frame("error", error={"type": kind, "message": CANARY})])
            wires.append(wire)
            return response(wire)
        return httpx2.Response(
            status,
            headers={"location": "https://other.invalid"},
            json={"type": "error", "error": {"type": kind, "message": CANARY}},
        )

    with caplog.at_level(logging.INFO):
        async with AnthropicProvider(config(), transport=httpx2.MockTransport(handle)) as provider:
            events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert events == [ResponseFailed(code=expected, retryable=attempts > 1)]
    assert len(requests) == attempts and all(w.closed for w in wires)
    assert CANARY not in repr(events) + caplog.text


async def test_explicit_credentials_and_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "foreign-canary")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unrelated.invalid")
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx2.ConnectError(CANARY)
        return response(WireStream(text_frames()))

    async with AnthropicProvider(config(), transport=httpx2.MockTransport(handle)) as provider:
        request = model_request(with_tools=True).model_copy(update={"remaining_tokens": 12})
        events = [e async for e in provider.stream(request, CancelToken())]
        assert CANARY not in repr(provider) + repr(provider.config) + request.model_dump_json()
    assert isinstance(events[-1], ResponseCompleted) and len(requests) == 2
    for outgoing in requests:
        assert str(outgoing.url) == "https://api.anthropic.com/v1/messages"
        assert outgoing.headers["x-api-key"] == CANARY
        assert "authorization" not in outgoing.headers
        assert "foreign-canary" not in str(outgoing.headers)
        assert outgoing.headers["accept-encoding"] == "identity"
        body = json.loads(outgoing.content)
        assert body["max_tokens"] == 12 and body["thinking"] == {"type": "disabled"}
        assert body["tool_choice"]["disable_parallel_tool_use"] is False
        assert CANARY not in outgoing.content.decode()


@pytest.mark.parametrize(
    "where", ["before_start", "connect", "backoff", "body", "error_body", "parent_task"]
)
async def test_cancel_each_wait_closes_owned_response(where: str) -> None:
    entered = asyncio.Event()
    wire = WireStream([], block=True)
    count = 0

    async def handle(_: httpx2.Request) -> httpx2.Response:
        nonlocal count
        count += 1
        if where == "connect":
            entered.set()
            await asyncio.Event().wait()
        if where == "backoff":
            entered.set()
            return httpx2.Response(429, json={"error": {"type": "rate_limit_error"}})
        if where == "error_body":
            return httpx2.Response(500, stream=wire)
        return response(wire)

    settings = config().model_copy(update={"retry_delay_seconds": 10})
    async with AnthropicProvider(settings, transport=httpx2.MockTransport(handle)) as provider:
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
        assert count == (0 if where == "before_start" else 1)
        if where in {"body", "error_body", "parent_task"}:
            assert wire.closed


@pytest.mark.parametrize("kind", ["normal", "http_error"])
async def test_total_timeout_across_consumer_tasks(kind: str) -> None:
    wire = WireStream([start()] if kind == "normal" else [], block=True)

    def handle(_: httpx2.Request) -> httpx2.Response:
        return response(wire) if kind == "normal" else httpx2.Response(500, stream=wire)

    async with AnthropicProvider(
        config(timeout_seconds=0.03), transport=httpx2.MockTransport(handle)
    ) as provider:
        events = []
        token = CancelToken()
        async with aclosing(provider.stream(model_request(), token)) as stream:
            while True:
                try:
                    events.append(await asyncio.wait_for(token.run(anext(stream)), 2))
                except StopAsyncIteration:
                    break
    assert events[-1] == ResponseFailed(code="transport", retryable=True) and wire.closed


@pytest.mark.parametrize(
    "kind", ["bytes", "frame", "chunks", "frames", "http_error", "compressed", "not_sse"]
)
async def test_transport_boundaries(kind: str) -> None:
    parts = [b"x" * 2048]
    limits: dict[str, Any] = {"max_response_bytes": 1024}
    if kind == "frame":
        limits = {"max_frame_bytes": 128}
    elif kind == "chunks":
        parts, limits = [frame("ping")] * 3, {"max_chunks": 2}
    elif kind == "frames":
        parts, limits = [frame("ping") * 3], {"max_chunks": 2}
    wire = WireStream(parts)

    def handle(_: httpx2.Request) -> httpx2.Response:
        if kind == "compressed":
            return httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=wire)
        if kind == "http_error":
            return httpx2.Response(500, stream=wire)
        if kind == "not_sse":
            return httpx2.Response(200, headers={"content-type": "application/json"}, stream=wire)
        return response(wire)

    async with AnthropicProvider(
        config(**limits), transport=httpx2.MockTransport(handle)
    ) as provider:
        events = [e async for e in provider.stream(model_request(), CancelToken())]
    assert events[-1] == ResponseFailed(code="invalid_provider_output") and wire.closed


@pytest.mark.parametrize("reason", ["max_tokens", "refusal", "pause_turn"])
async def test_failed_finish_preserves_totals_without_tool_dispatch(
    tmp_path: Path, reason: str
) -> None:
    parts = tool_frames("{")[:-2] + stop(reason)
    tools = RecordingTools()
    async with AnthropicProvider(
        config(), transport=httpx2.MockTransport(lambda _: response(WireStream(parts)))
    ) as provider:
        async with AgentRuntime(SQLiteSessionStore(tmp_path / "s.db"), provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert turn.status == TurnStatus.FAILED and tools.calls == []
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)


@pytest.mark.parametrize("approval", [False, True])
async def test_kernel_cross_provider_resume_pairing_and_replay(
    tmp_path: Path, approval: bool
) -> None:
    tools = RecordingTools(approval=approval)
    store = SQLiteSessionStore(tmp_path / "cross.db")
    requests: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return response(WireStream(tool_frames() if len(requests) == 1 else text_frames()))

    async with AnthropicProvider(config(), transport=httpx2.MockTransport(handle)) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id, "读取", request_id="r", budget=Budget(max_tokens=100)
            )
    if approval:
        assert turn.status == TurnStatus.WAITING_APPROVAL and tools.calls == []
        checkpoint = next(
            i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
        )
        async with AnthropicProvider(config(), transport=httpx2.MockTransport(handle)) as provider:
            async with AgentRuntime(store, provider, tools) as runtime:
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    checkpoint.approval_id,
                    fingerprint=checkpoint.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="测试"),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
    assert turn.status == TurnStatus.COMPLETED and len(tools.calls) == 1
    assert turn.usage == Usage(input_tokens=20, output_tokens=4)
    assert [request["max_tokens"] for request in requests] == [100, 88]
    history = requests[-1]["messages"]
    assert history[-2]["content"][0]["id"] == history[-1]["content"][0]["tool_use_id"]
    seen_openai: list[dict[str, Any]] = []

    def openai_handle(request: httpx.Request) -> httpx.Response:
        seen_openai.append(json.loads(request.content))
        return openai_wire.response(openai_wire.WireStream(openai_wire.text_frames()))

    async with OpenAIChatProvider(
        OpenAIChatConfig(model="test", api_key_env="HARNESSIX_TEST_ANTHROPIC_KEY"),
        transport=httpx.MockTransport(openai_handle),
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            next_turn = await runtime.run_turn(thread.thread_id, "总结", request_id="next")
            assert next_turn.status == TurnStatus.COMPLETED
    assert len(tools.calls) == 1 and len(seen_openai) == 1
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)
    assert CANARY not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("limit", ["text", "calls", "parallel"])
async def test_semantic_budgets_and_capabilities(limit: str) -> None:
    parts = (
        text_frames()
        if limit == "text"
        else [start(), *tool_block(), *tool_block(1), *stop("tool_use")]
    )
    budget = Budget(max_output_chars=1) if limit == "text" else Budget(max_tool_calls_per_step=1)
    if limit == "parallel":
        budget = Budget()
    async with AnthropicProvider(
        config(capabilities=ChatCapabilities(parallel_tool_calls=limit != "parallel")),
        transport=httpx2.MockTransport(lambda _: response(WireStream(parts))),
    ) as provider:
        request = model_request(with_tools=True).model_copy(update={"budget": budget})
        events = [e async for e in provider.stream(request, CancelToken())]
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)


async def test_concurrency_and_closed_lifecycle() -> None:
    async with AnthropicProvider(
        config(), transport=httpx2.MockTransport(lambda _: response(WireStream(text_frames())))
    ) as provider:

        async def consume() -> list[Any]:
            return [e async for e in provider.stream(model_request(), CancelToken())]

        results = await asyncio.gather(*(consume() for _ in range(4)))
        assert all(isinstance(events[-1], ResponseCompleted) for events in results)
    assert [e async for e in provider.stream(model_request(), CancelToken())] == [
        ResponseFailed(code="invalid_request")
    ]


async def test_missing_tool_capability_sends_no_request() -> None:
    async with AnthropicProvider(
        config(capabilities=ChatCapabilities(tool_calls=False, parallel_tool_calls=False)),
        transport=httpx2.MockTransport(lambda _: pytest.fail("不支持的请求不能发网")),
    ) as provider:
        assert [
            e async for e in provider.stream(model_request(with_tools=True), CancelToken())
        ] == [ResponseFailed(code="invalid_request")]


async def test_switch_provider_at_durable_approval_boundary(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "switch.db")
    tools = RecordingTools(approval=True)
    async with AnthropicProvider(
        config(), transport=httpx2.MockTransport(lambda _: response(WireStream(tool_frames())))
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "读取", request_id="r")
    checkpoint = next(
        i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
    )
    async with OpenAIChatProvider(
        OpenAIChatConfig(model="test", api_key_env="HARNESSIX_TEST_ANTHROPIC_KEY"),
        transport=httpx.MockTransport(
            lambda _: openai_wire.response(openai_wire.WireStream(openai_wire.text_frames()))
        ),
    ) as provider:
        async with AgentRuntime(store, provider, tools) as runtime:
            await runtime.reply_approval(
                thread.thread_id,
                turn.turn_id,
                checkpoint.approval_id,
                fingerprint=checkpoint.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="测试"),
            )
            completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
    assert completed.status == TurnStatus.COMPLETED and len(tools.calls) == 1
    assert completed.usage == Usage(input_tokens=20, output_tokens=4)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
