"""离线目录拒绝、整组结构校验及闭合历史映射的正负控。"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

import httpx
import httpx2
import pytest
from openai.types.chat import ChatCompletionChunk
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, FailureCategory
from harnessix.agent.models import (
    Item,
    ItemContent,
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Usage,
)
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.domain.models import EffectClass
from harnessix.models import _history
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._anthropic_stream import AnthropicStream, validate_event
from harnessix.models._bounded_http import InvalidWireData
from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models._chat_stream import CallParts, ChatStream, _complete_calls
from harnessix.models._history import InvalidModelRequest, messages_for, tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
    TextCompleted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.contracts.provider import model_request
from tests.models import anthropic_wire, wire

Provider = Literal["chat", "anthropic"]
MARKER = "harnessix_rejected_tool_v1"
UNKNOWN = "unknown-tool-NAME-CANARY"
ARGUMENTS = '{"秘密":"ARGUMENT-CANARY-中文"}'
PROPOSALS = (ToolCallCompleted, ToolCallRejected)


def _call(index: int, name: str, arguments: str = "{}") -> dict[str, Any]:
    return {
        "index": index,
        "id": f"wire-call-{index}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _values(provider: Provider, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if provider == "chat":
        return [
            wire.chunk({"tool_calls": calls}),
            wire.chunk(finish="tool_calls"),
            wire.chunk(usage=True),
        ]
    values = [json.loads(anthropic_wire.start().split(b"data: ", 1)[1])]
    for call in calls:
        values.extend(
            [
                {
                    "type": "content_block_start",
                    "index": call["index"],
                    "content_block": {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": call["index"],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": call["function"]["arguments"],
                    },
                },
                {"type": "content_block_stop", "index": call["index"]},
            ]
        )
    values.extend(
        json.loads(part.split(b"data: ", 1)[1]) for part in anthropic_wire.stop("tool_use")
    )
    return values


def _frames(provider: Provider, values: list[dict[str, Any]]) -> list[bytes]:
    if provider == "chat":
        return [*(wire.frame(value) for value in values), b"data: [DONE]\n\n"]
    return [
        anthropic_wire.frame(value["type"], **{k: v for k, v in value.items() if k != "type"})
        for value in values
    ]


async def _collect(
    provider: Provider,
    parts: list[bytes],
    *,
    request: ModelRequest | None = None,
    parallel: bool = True,
    fail: bool = False,
    limits: dict[str, Any] | None = None,
) -> list[ProviderEvent]:
    request = request or model_request(with_tools=True)
    limits = limits or {}
    requests = 0

    if provider == "chat":
        stream = wire.WireStream(parts, fail=fail)

        def handle_chat(_: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return wire.response(stream)

        async with OpenAIChatProvider(
            OpenAIChatConfig(
                model="test-model",
                capabilities=ChatCapabilities(parallel_tool_calls=parallel),
                **limits,
            ),
            transport=httpx.MockTransport(handle_chat),
            api_key="fixture-key",
        ) as adapter:
            events = [event async for event in adapter.stream(request, CancelToken())]
    else:
        anthropic_stream = anthropic_wire.WireStream(parts, fail=fail)

        def handle_anthropic(_: httpx2.Request) -> httpx2.Response:
            nonlocal requests
            requests += 1
            return anthropic_wire.response(anthropic_stream)

        async with AnthropicProvider(
            AnthropicConfig(
                model="test-model",
                capabilities=ChatCapabilities(parallel_tool_calls=parallel),
                **limits,
            ),
            transport=httpx2.MockTransport(handle_anthropic),
            api_key="fixture-key",
        ) as anthropic_adapter:
            events = [event async for event in anthropic_adapter.stream(request, CancelToken())]
        assert anthropic_stream.closed
    if provider == "chat":
        assert stream.closed
    assert requests == 1
    assert len([event for event in events if isinstance(event, ModelAttemptStarted)]) == 1
    return events


@pytest.fixture(autouse=True)
def _fixture_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize(
    "registered", [(True,), (False,), (True, True), (False, False), (True, False), (False, True)]
)
async def test_complete_group_preserves_order_usage_and_rejects_without_payload(
    provider: Provider, registered: tuple[bool, ...]
) -> None:
    calls = [
        _call(index, tool_alias("test.read") if known else UNKNOWN, ARGUMENTS)
        for index, known in enumerate(registered)
    ]
    events = await _collect(provider, _frames(provider, _values(provider, calls)))
    proposals = [event for event in events if isinstance(event, PROPOSALS)]
    assert len(proposals) == len(calls)
    assert [event.call_id for event in proposals] == [call["id"] for call in calls]
    for event, known in zip(proposals, registered, strict=True):
        if known:
            assert event == ToolCallCompleted(
                call_id=event.call_id, tool="test.read", arguments=json.loads(ARGUMENTS)
            )
        else:
            assert event.model_dump() == {
                "type": "tool_call_rejected",
                "call_id": event.call_id,
                "reason": "unregistered_tool",
                "argument_chars": len(ARGUMENTS),
            }
            assert type(event.argument_chars) is int
    assert events[-1] == ResponseCompleted(
        finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=2)
    )
    observations = [event for event in events if isinstance(event, ModelUsageObserved)]
    assert observations[-1].usage.completeness == "complete"
    assert observations[-1].usage.input_tokens == 10
    assert observations[-1].usage.output_tokens == 2
    terminal = next(event for event in events if isinstance(event, ModelAttemptFinished))
    assert terminal.outcome == "completed" and terminal.error is None
    assert events.index(terminal) < events.index(proposals[0])
    serialized = "".join(event.model_dump_json() for event in events)
    assert UNKNOWN not in serialized
    if not any(registered):
        assert "ARGUMENT-CANARY" not in serialized


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
def test_no_proposal_is_released_before_whole_stream_completion(provider: Provider) -> None:
    request = model_request(with_tools=True)
    names = {tool_alias("test.read"): "test.read"}
    state = (
        ChatStream(request, names, parallel=True, attempt_id=uuid4())
        if provider == "chat"
        else AnthropicStream(request, names, parallel=True, attempt_id=uuid4())
    )
    calls = [_call(0, UNKNOWN, ARGUMENTS), _call(1, tool_alias("test.read"))]
    for value in _values(provider, calls):
        event = (
            ChatCompletionChunk.model_validate(value, strict=True)
            if provider == "chat"
            else validate_event(value)
        )
        assert not any(isinstance(result, PROPOSALS) for result in state.feed(event))
    events = state.finish(seen_done=True) if isinstance(state, ChatStream) else state.finish()
    assert [type(event) for event in events] == [
        ToolCallRejected,
        ToolCallCompleted,
        ResponseCompleted,
    ]


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
async def test_fragmented_rejection_preserves_identity_and_counts_all_raw_argument_chars(
    provider: Provider,
) -> None:
    fragments = [' {"秘密":', '"ARGUMENT-CANARY-中文"', "} "]
    values = _values(provider, [_call(0, UNKNOWN, fragments[0])])
    if provider == "chat":
        values[1:1] = [
            wire.chunk({"tool_calls": [{"index": 0, "id": "", "function": {"arguments": part}}]})
            for part in fragments[1:]
        ]
    else:
        values[3:3] = [
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": part},
            }
            for part in fragments[1:]
        ]
    request = model_request(with_tools=True)
    count = sum(len(part) for part in fragments)
    request = request.model_copy(
        update={"budget": request.budget.model_copy(update={"max_output_chars": count})}
    )
    events = await _collect(provider, _frames(provider, values), request=request)
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=count)
    ]
    assert isinstance(events[-1], ResponseCompleted)
    assert "ARGUMENT-CANARY" not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
async def test_text_members_and_proposal_order_are_preserved(provider: Provider) -> None:
    values = _values(provider, [_call(0, UNKNOWN), _call(1, tool_alias("test.read"))])
    if provider == "chat":
        values[0]["choices"][0]["delta"]["content"] = "说明"
    else:
        for value in values[1:-2]:
            value["index"] += 1
        values[1:1] = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "说明"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
    events = await _collect(provider, _frames(provider, values))
    completed = [event for event in events if isinstance(event, (*PROPOSALS, TextCompleted))]
    assert [type(event) for event in completed] == [
        TextCompleted,
        ToolCallRejected,
        ToolCallCompleted,
    ]
    assert completed[0].text == "说明"
    assert [event.call_id for event in completed[1:]] == ["wire-call-0", "wire-call-1"]


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
async def test_absent_current_catalog_does_not_infer_authority_from_known_history(
    provider: Provider,
) -> None:
    known = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="old-provider-id",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
    )
    request = model_request()
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                _item(known),
                _item(ToolResultContent(call_id=known.call_id, outcome="succeeded")),
            )
        }
    )
    body, names = _build(provider, request)
    assert names == {} and "tools" not in body
    events = await _collect(
        provider,
        _frames(provider, _values(provider, [_call(0, tool_alias("test.read"))])),
        request=request,
    )
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=2)
    ]


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
async def test_unknown_call_identity_keeps_original_256_character_boundary(
    provider: Provider,
) -> None:
    call = _call(0, UNKNOWN)
    call["id"] = "i" * 256
    events = await _collect(provider, _frames(provider, _values(provider, [call])))
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id=call["id"], argument_chars=2)
    ]


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize("length", [1, 256])
async def test_unknown_name_accepts_only_nonempty_bounded_strings(
    provider: Provider, length: int
) -> None:
    name = "界" * length
    events = await _collect(provider, _frames(provider, _values(provider, [_call(0, name)])))
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=2)
    ]
    assert name not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize("name", ["", "界" * 257], ids=["empty", "overlength"])
def test_invalid_name_is_not_retained_or_released(provider: Provider, name: str) -> None:
    request = model_request(with_tools=True)
    names = {tool_alias("test.read"): "test.read"}
    if provider == "chat":
        state = ChatStream(request, names, parallel=True, attempt_id=uuid4())
        value = ChatCompletionChunk.model_validate(
            wire.chunk({"tool_calls": [_call(0, name, ARGUMENTS)]}), strict=True
        )
        if name == "":
            # 空增量与缺省一样可等待后片；整流结束仍缺名时才判定失败。
            for raw in _values("chat", [_call(0, name, ARGUMENTS)]):
                events = state.feed(ChatCompletionChunk.model_validate(raw, strict=True))
                assert not any(isinstance(event, PROPOSALS) for event in events)
            with pytest.raises(ChatProtocolError) as caught:
                state.finish(seen_done=True)
        else:
            with pytest.raises(ChatProtocolError) as caught:
                state.feed(value)
        assert caught.value.reason is ChatProtocolReason.TOOL_NAME_UNKNOWN
        assert str(caught.value) == "Chat终态不符合协议"
        assert all(call.name is None for call in state._calls.values())
    else:
        anthropic_state = AnthropicStream(request, names, parallel=True, attempt_id=uuid4())
        values = _values(provider, [_call(0, name, ARGUMENTS)])
        anthropic_state.feed(validate_event(values[0]))
        with pytest.raises(InvalidWireData, match="工具 Block 身份或能力无效"):
            anthropic_state.feed(validate_event(values[1]))
        assert anthropic_state._blocks == {} and anthropic_state._call_ids == set()


@pytest.mark.parametrize("name", [None, "", "界" * 257], ids=["missing", "empty", "overlength"])
def test_standalone_chat_group_validation_rejects_invalid_name(name: str | None) -> None:
    parts = CallParts(call_id="wire-call-0", name=name, arguments=ARGUMENTS, type="function")
    with pytest.raises(ChatProtocolError) as caught:
        _complete_calls({0: parts}, {tool_alias("test.read"): "test.read"})
    assert caught.value.reason is ChatProtocolReason.TOOL_NAME_UNKNOWN
    assert str(caught.value) == "Chat终态不符合协议"


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
async def test_unknown_argument_chars_reaches_original_million_character_budget(
    provider: Provider,
) -> None:
    arguments = '{"x":"' + "x" * (1_000_000 - len('{"x":""}')) + '"}'
    request = model_request()
    request = request.model_copy(
        update={"budget": request.budget.model_copy(update={"max_output_chars": 1_000_000})}
    )
    events = await _collect(
        provider,
        _frames(provider, _values(provider, [_call(0, UNKNOWN, arguments)])),
        request=request,
        limits={"max_frame_bytes": 1_048_576},
    )
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=1_000_000)
    ]


@pytest.mark.parametrize("arguments", [None, ""])
async def test_anthropic_unknown_empty_start_object_does_not_fabricate_argument_chars(
    arguments: str | None,
) -> None:
    values = _values("anthropic", [_call(0, UNKNOWN, "")])
    if arguments is None:
        values.pop(2)
    events = await _collect("anthropic", _frames("anthropic", values))
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=0)
    ]


async def test_chat_unknown_missing_arguments_is_not_an_empty_object() -> None:
    events = await _collect("chat", _frames("chat", _values("chat", [_call(0, UNKNOWN, "")])))
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(event, (*PROPOSALS, ResponseCompleted)) for event in events)


@pytest.mark.parametrize("fault", ["id_drift", "name_drift", "missing_type", "wrong_type"])
async def test_chat_unknown_call_does_not_relax_incremental_identity_or_function_type(
    fault: str,
) -> None:
    values = _values("chat", [_call(0, UNKNOWN)])
    if fault in {"id_drift", "name_drift"}:
        part = {"index": 0}
        part.update(
            {"id": "changed-id"} if fault == "id_drift" else {"function": {"name": "changed-name"}}
        )
        values.insert(1, wire.chunk({"tool_calls": [part]}))
    elif fault == "missing_type":
        values[0]["choices"][0]["delta"]["tool_calls"][0].pop("type")
    else:
        values[0]["choices"][0]["delta"]["tool_calls"][0]["type"] = "other"
    events = await _collect("chat", _frames("chat", values))
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(event, (*PROPOSALS, ResponseCompleted)) for event in events)


@pytest.mark.parametrize("fault", ["initial_input", "toolset", "server_caller", "unclosed"])
async def test_anthropic_unknown_block_does_not_relax_original_tool_capabilities(
    fault: str,
) -> None:
    values = _values("anthropic", [_call(0, UNKNOWN)])
    block = values[1]["content_block"]
    if fault == "initial_input":
        block["input"] = {"x": 1}
    elif fault == "toolset":
        block["toolset_name"] = "fixture-toolset"
    elif fault == "server_caller":
        block["caller"] = {"type": "code_execution_20250825", "tool_id": "server-id"}
    else:
        values.pop(3)
    events = await _collect("anthropic", _frames("anthropic", values))
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(event, (*PROPOSALS, ResponseCompleted)) for event in events)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize(
    "name", [UNKNOWN, "test.read", MARKER, "hx_test_read_wrong_digest", "Hx_test_read"]
)
async def test_only_exact_current_advertised_alias_can_be_completed(
    provider: Provider, name: str
) -> None:
    events = await _collect(provider, _frames(provider, _values(provider, [_call(0, name)])))
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=2)
    ]
    assert isinstance(events[-1], ResponseCompleted)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
async def test_name_registered_under_history_marker_does_not_advertise_or_execute_marker(
    provider: Provider,
) -> None:
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={"tools": (request.tools[0].model_copy(update={"name": MARKER}),)}
    )
    body, names = _build(provider, request)
    assert names == {tool_alias(MARKER): MARKER} and MARKER not in names
    advertised = (
        body["tools"][0]["function"]["name"] if provider == "chat" else body["tools"][0]["name"]
    )
    assert advertised == tool_alias(MARKER)
    events = await _collect(
        provider, _frames(provider, _values(provider, [_call(0, MARKER)])), request=request
    )
    assert [event for event in events if isinstance(event, PROPOSALS)] == [
        ToolCallRejected(call_id="wire-call-0", argument_chars=2)
    ]


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize("unknown_first", [False, True])
@pytest.mark.parametrize(
    "arguments", ["{", "[]", "null", "1", '{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}']
)
async def test_malformed_json_in_either_member_releases_no_group(
    provider: Provider, unknown_first: bool, arguments: str
) -> None:
    calls = [
        _call(0, UNKNOWN if unknown_first else tool_alias("test.read"), ARGUMENTS),
        _call(1, tool_alias("test.read") if unknown_first else UNKNOWN, arguments),
    ]
    events = await _collect(provider, _frames(provider, _values(provider, calls)))
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(event, (*PROPOSALS, ResponseCompleted)) for event in events)
    assert UNKNOWN not in "".join(event.model_dump_json() for event in events)
    assert "ARGUMENT-CANARY" not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
def test_catalog_is_not_consulted_until_every_member_is_structurally_valid(
    provider: Provider,
) -> None:
    class StructuralOnlyCatalog(dict[str, str]):
        def __contains__(self, key: object) -> bool:
            pytest.fail("整组结构未通过前不得查询工具目录")

    request = model_request(with_tools=True)
    catalog = StructuralOnlyCatalog({tool_alias("test.read"): "test.read"})
    state = (
        ChatStream(request, catalog, parallel=True, attempt_id=uuid4())
        if provider == "chat"
        else AnthropicStream(request, catalog, parallel=True, attempt_id=uuid4())
    )
    calls = [_call(0, UNKNOWN, ARGUMENTS), _call(1, tool_alias("test.read"), "[]")]
    for value in _values(provider, calls):
        event = (
            ChatCompletionChunk.model_validate(value, strict=True)
            if provider == "chat"
            else validate_event(value)
        )
        state.feed(event)
    with pytest.raises(InvalidWireData):
        if isinstance(state, ChatStream):
            state.finish(seen_done=True)
        else:
            state.finish()


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_id",
        "long_id",
        "duplicate_id",
        "index_gap",
        "negative_index",
        "empty_name",
        "long_name",
        "missing_name",
        "wrong_name_type",
        "missing_usage",
        "bad_usage",
        "missing_terminal",
        "wrong_finish",
        "duplicate_finish",
        "extra_event",
    ],
)
async def test_structural_faults_do_not_become_recoverable_rejections(
    provider: Provider, fault: str
) -> None:
    calls = [_call(0, tool_alias("test.read")), _call(1, UNKNOWN, ARGUMENTS)]
    if fault == "missing_id":
        calls[1]["id"] = ""
    elif fault == "long_id":
        calls[1]["id"] = "i" * 257
    elif fault == "duplicate_id":
        calls[1]["id"] = calls[0]["id"]
    elif fault in {"index_gap", "negative_index"}:
        calls[1]["index"] = 2 if fault == "index_gap" else -1
    elif fault in {"empty_name", "missing_name", "wrong_name_type"}:
        calls[1]["function"]["name"] = {
            "empty_name": "",
            "missing_name": None,
            "wrong_name_type": 1,
        }[fault]
    elif fault == "long_name":
        calls[1]["function"]["name"] = "界" * 257
    values = _values(provider, calls)
    if fault == "missing_usage":
        if provider == "chat":
            values.pop()
        else:
            del values[0]["message"]["usage"]["cache_read_input_tokens"]
    elif fault == "bad_usage":
        values[-1 if provider == "chat" else -2]["usage"]["output_tokens"] = -1
        if provider == "chat":
            values[-1]["usage"]["prompt_tokens"] = -1
    elif fault == "missing_terminal":
        if provider == "anthropic":
            values.pop()
    elif fault == "wrong_finish":
        if provider == "chat":
            values[-2]["choices"][0]["finish_reason"] = "stop"
        else:
            values[-2]["delta"]["stop_reason"] = "end_turn"
    elif fault == "duplicate_finish":
        values.insert(-1, values[-2])
    elif fault == "extra_event":
        values.append(wire.chunk() if provider == "chat" else values[-1])
    parts = _frames(provider, values)
    if provider == "chat" and fault == "missing_terminal":
        parts.pop()
    events = await _collect(provider, parts)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(event, (*PROPOSALS, ResponseCompleted)) for event in events)
    assert UNKNOWN not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize("fault", ["output", "calls", "parallel", "transport", "frame", "chunks"])
async def test_rejection_preserves_original_budgets_and_transport_guards(
    provider: Provider, fault: str
) -> None:
    request = model_request(with_tools=True)
    calls = [_call(0, UNKNOWN, ARGUMENTS), _call(1, tool_alias("test.read"))]
    if fault in {"output", "calls"}:
        request = request.model_copy(
            update={
                "budget": request.budget.model_copy(
                    update={"max_output_chars": 2}
                    if fault == "output"
                    else {"max_tool_calls_per_step": 1}
                )
            }
        )
    limits = (
        {"max_frame_bytes": 128}
        if fault == "frame"
        else {"max_chunks": 1}
        if fault == "chunks"
        else {}
    )
    parts = _frames(provider, _values(provider, calls))
    if provider == "chat" and fault == "transport":
        # 故障发生在原传输终态之前；SDK 读取到 [DONE] 后不再读取底层尾部。
        parts.pop()
    events = await _collect(
        provider,
        parts,
        request=request,
        parallel=fault != "parallel",
        fail=fault == "transport",
        limits=limits,
    )
    expected = (
        ResponseFailed(code="transport", retryable=True)
        if fault == "transport"
        else ResponseFailed(code="invalid_provider_output")
    )
    assert events[-1] == expected
    assert not any(isinstance(event, (*PROPOSALS, ResponseCompleted)) for event in events)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
@pytest.mark.parametrize("finish", ["limit", "filter", "unknown"])
async def test_unsuccessful_terminal_never_releases_unknown_or_known_calls(
    provider: Provider, finish: str
) -> None:
    values = _values(provider, [_call(0, UNKNOWN, "{"), _call(1, tool_alias("test.read"))])
    if provider == "chat":
        values[-2]["choices"][0]["finish_reason"] = {
            "limit": "length",
            "filter": "content_filter",
            "unknown": "future_finish",
        }[finish]
    else:
        values[-2]["delta"]["stop_reason"] = {
            "limit": "max_tokens",
            "filter": "refusal",
            "unknown": "pause_turn",
        }[finish]
    events = await _collect(provider, _frames(provider, values))
    assert not any(isinstance(event, PROPOSALS) for event in events)
    if provider == "chat" and finish == "unknown":
        assert events[-1] == ResponseFailed(code="invalid_provider_output")
    else:
        reason = {"limit": "max_output_tokens", "filter": "content_filter", "unknown": "unknown"}[
            finish
        ]
        assert events[-1] == ResponseCompleted(
            finish_reason=reason, usage=Usage(input_tokens=10, output_tokens=2)
        )


@pytest.mark.parametrize("argument_chars", [0, 1_000_000])
def test_rejection_argument_count_accepts_integer_bounds(argument_chars: int) -> None:
    event = ToolCallRejected(call_id="bounded-id", argument_chars=argument_chars)
    assert type(event.argument_chars) is int and event.argument_chars == argument_chars


@pytest.mark.parametrize("argument_chars", [-1, 1_000_001, True, False, "0", 1.0, None])
def test_rejection_argument_count_is_strict_and_bounded(argument_chars: Any) -> None:
    with pytest.raises(ValidationError):
        ToolCallRejected(call_id="bounded-id", argument_chars=argument_chars)


@pytest.mark.parametrize("extra", [{"tool": UNKNOWN}, {"arguments": {}}, {"reason": "other"}])
def test_rejection_contract_is_closed(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ToolCallRejected.model_validate({"call_id": "bounded-id", "argument_chars": 0, **extra})


def _item(content: ItemContent, status: ItemStatus = ItemStatus.COMPLETED) -> Item:
    return Item(item_id=uuid4(), status=status, content=content)


def _rejection() -> ToolCallRejectionContent:
    return ToolCallRejectionContent(
        call_id=uuid4(), provider_call_id="reused-provider-id", model_step=1
    )


def _result(rejected: ToolCallRejectionContent) -> ToolResultContent:
    return ToolResultContent(
        call_id=rejected.call_id,
        outcome="failed",
        error=AgentFailure(code="unknown_tool", message="工具未注册", retryable=False),
    )


def _build(provider: Provider, request: ModelRequest) -> tuple[dict[str, Any], dict[str, str]]:
    if provider == "chat":
        return chat_request(request, OpenAIChatConfig(model="test-model"))
    return anthropic_request(request, AnthropicConfig(model="test-model"))


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
def test_rejection_history_uses_fixed_marker_and_original_uuid_pairing(
    provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _rejection(), _rejection()
    request = model_request().model_copy(
        update={
            "history": (
                *model_request().history,
                _item(first),
                _item(second),
                _item(_result(second)),
                _item(_result(first)),
            )
        }
    )

    def forbidden_alias(_: str) -> str:
        pytest.fail("拒绝历史不得重新计算工具别名")

    monkeypatch.setattr(_history, "tool_alias", forbidden_alias)
    body, names = _build(provider, request)
    assert names == {} and "tools" not in body
    assert "reused-provider-id" not in json.dumps(body)
    if provider == "chat":
        uses = body["messages"][1]["tool_calls"]
        assert [call["function"] for call in uses] == [
            {"name": MARKER, "arguments": "{}"},
            {"name": MARKER, "arguments": "{}"},
        ]
        use_ids = [call["id"] for call in uses]
        results = body["messages"][2:]
        result_ids = [result["tool_call_id"] for result in results]
    else:
        uses = body["messages"][1]["content"]
        assert all(call["name"] == MARKER and call["input"] == {} for call in uses)
        use_ids = [call["id"] for call in uses]
        results = body["messages"][2]["content"]
        assert all(result["is_error"] for result in results)
        result_ids = [result["tool_use_id"] for result in results]
    assert use_ids == ["call_" + first.call_id.hex, "call_" + second.call_id.hex]
    assert result_ids == list(reversed(use_ids))
    for result in results:
        data = json.loads(result["content"])
        assert data["outcome"] == "failed" and data["output"] is None
        assert data["error"] == {
            "code": "unknown_tool",
            "message": "工具未注册",
            "category": "tool",
            "retryable": False,
        }


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
def test_mixed_history_preserves_known_alias_and_advertises_only_current_tools(
    provider: Provider,
) -> None:
    rejected = _rejection()
    known = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="known-provider-id",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
        arguments={"path": "中文.txt"},
    )
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                _item(known),
                _item(rejected),
                _item(_result(rejected)),
                _item(ToolResultContent(call_id=known.call_id, outcome="succeeded", output=1)),
            )
        }
    )
    body, names = _build(provider, request)
    assert names == {tool_alias("test.read"): "test.read"}
    assert MARKER not in names and MARKER not in json.dumps(body["tools"])
    uses = body["messages"][1]["tool_calls" if provider == "chat" else "content"]
    if provider == "chat":
        assert [call["function"]["name"] for call in uses] == [tool_alias("test.read"), MARKER]
        assert json.loads(uses[0]["function"]["arguments"]) == known.arguments
    else:
        assert [call["name"] for call in uses] == [tool_alias("test.read"), MARKER]
        assert uses[0]["input"] == known.arguments


@pytest.mark.parametrize(
    "fault", ["missing", "orphan", "duplicate", "reused", "unfinished", "interrupted", "collision"]
)
def test_rejection_history_requires_completed_unique_closed_pairing(fault: str) -> None:
    first, second = _rejection(), _rejection()
    result = _result(first)
    known = ToolCallContent(
        call_id=first.call_id,
        provider_call_id="known-id",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
    )
    histories = {
        "missing": (_item(first),),
        "orphan": (_item(result),),
        "duplicate": (_item(first), _item(result), _item(result)),
        "reused": (_item(first), _item(result), _item(first), _item(result)),
        "unfinished": (_item(first, ItemStatus.STARTED), _item(result)),
        "interrupted": (
            _item(first),
            _item(second),
            _item(result),
            _item(TextContent(kind="assistant_message", text="插入")),
            _item(_result(second)),
        ),
        "collision": (_item(known), _item(first), _item(result)),
    }
    request = model_request().model_copy(update={"history": histories[fault]})
    with pytest.raises(InvalidModelRequest):
        messages_for(request)


@pytest.mark.parametrize("mapping", ["history", "chat", "anthropic"])
@pytest.mark.parametrize(
    "fault",
    [
        "succeeded",
        "output",
        "action_id",
        "patch",
        "patch_batch",
        "process",
        "trusted_action",
        "diff_artifact",
        "missing_error",
        "error_code",
        "error_message",
        "retryable",
        "category",
    ],
)
def test_rejection_history_requires_fixed_effect_free_result_before_serialization(
    mapping: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected = _rejection()
    result = _result(rejected)
    canary = "REJECTED_RESULT_RAW_MUST_NOT_BE_PUBLISHED"
    if fault == "succeeded":
        result = ToolResultContent(call_id=rejected.call_id, outcome="succeeded")
    elif fault == "output":
        result = result.model_copy(update={"output": {"raw": canary}})
    elif fault == "action_id":
        result = result.model_copy(update={"action_id": uuid4()})
    elif fault in {"patch", "patch_batch", "process", "trusted_action", "diff_artifact"}:
        # 模拟独立调用者通过 model_copy 注入未再次校验的效果，必须在序列化前拦截。
        result = result.model_copy(update={fault: {"raw": canary}})
    elif fault == "missing_error":
        result = result.model_copy(update={"error": None})
    else:
        updates = {
            "error_code": {"code": canary},
            "error_message": {"message": canary},
            "retryable": {"retryable": True},
            "category": {"category": FailureCategory.INTERNAL},
        }
        result = result.model_copy(update={"error": result.error.model_copy(update=updates[fault])})
    request = model_request().model_copy(
        update={
            "history": (
                *model_request().history,
                _item(rejected),
                _item(_result(rejected)).model_copy(update={"content": result}),
            )
        }
    )

    def forbidden_serialization(_: object) -> str:
        pytest.fail("无效拒绝结果不得进入历史序列化")

    monkeypatch.setattr(_history, "encode_json", forbidden_serialization)
    with pytest.raises(InvalidModelRequest) as caught:
        if mapping == "history":
            messages_for(request)
        else:
            _build(mapping, request)
    assert str(caught.value) == "拒绝工具结果不符合固定合同"
    assert canary not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
def test_tool_capability_guard_includes_rejection_before_history_mapping(
    provider: Provider,
) -> None:
    request = model_request().model_copy(update={"history": (_item(_rejection()),)})
    capabilities = ChatCapabilities(tool_calls=False, parallel_tool_calls=False)
    with pytest.raises(InvalidModelRequest, match="不支持工具调用"):
        if provider == "chat":
            chat_request(request, OpenAIChatConfig(model="test-model", capabilities=capabilities))
        else:
            anthropic_request(
                request, AnthropicConfig(model="test-model", capabilities=capabilities)
            )
