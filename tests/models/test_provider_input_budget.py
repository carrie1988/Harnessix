"""输入估算只读复用出站 mapper，不创建第二套请求 JSON。"""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.model_input_budget import INCREMENTAL_REQUEST_OVERHEAD_TOKENS
from harnessix.agent.models import TextContent, ToolResultContent
from harnessix.models import openai_chat
from harnessix.models._chat_input_anchor import CompletedChatInput
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import InvalidModelRequest, encode_json, tool_alias
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import ModelInputTokenEstimator
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.contracts.provider import model_request
from tests.models.test_chat_mapping import call, item
from tests.models.wire import WireStream, response, text_frames


def appended_request(request):
    return request.model_copy(
        update={
            "step": request.step + 1,
            "history": (
                *request.history,
                item(TextContent(kind="assistant_message", text='已读"\\\n🙂' * 30)),
            ),
        }
    )


def anchor_fixture():
    provider = pure_provider()
    request = model_request(with_tools=True).model_copy(update={"instructions": "约束" * 1000})
    body, _ = build_request(request, provider.config)
    provider._completed_input = CompletedChatInput.capture(request, body, provider.config)
    return provider, request, appended_request(request)


async def test_completed_stream_reuses_exact_wrapper_and_close_clears_proof():
    async with OpenAIChatProvider(
        OpenAIChatConfig(model="test-model"),
        api_key="offline-fixture",
        transport=httpx.MockTransport(lambda _: response(WireStream(text_frames()))),
    ) as provider:
        previous = model_request(with_tools=True).model_copy(update={"instructions": "约束" * 1000})
        current = appended_request(previous)
        before = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
        assert [event async for event in provider.stream(previous, CancelToken())]
        after = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
        assert provider._completed_input is not None
        assert 512 <= after < before - 6000
        # 未给出已验证历史前缀时，仍必须使用完整 wire 上界。
        full_body, _ = build_request(current, provider.config)
        assert provider.estimate_input_tokens(current) == len(encode_json(full_body).encode()) + 512
    assert provider._completed_input is None


@pytest.mark.parametrize("drift", ["thread", "turn", "step", "prefix", "config"])
def test_wrapper_proof_mismatch_falls_back_without_io(drift):
    from uuid import uuid4

    provider, previous, current = anchor_fixture()
    if drift == "thread":
        current = current.model_copy(update={"thread_id": uuid4()})
    elif drift == "turn":
        current = current.model_copy(update={"turn_id": uuid4()})
    elif drift == "step":
        current = current.model_copy(update={"step": 3})
    elif drift == "prefix":
        current = current.model_copy(
            update={
                "history": (
                    item(TextContent(kind="user_message", text="改写")),
                    *current.history[1:],
                )
            }
        )
    else:
        provider.config = provider.config.model_copy(update={"enable_thinking": False})
    guarded = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
    provider._completed_input = None
    assert guarded == provider.estimate_input_tokens(current, prefix_items=len(previous.history))


@pytest.mark.parametrize("drift", ["instructions", "tools", "both"])
def test_changed_wrappers_are_charged_in_full(drift):
    provider, previous, current = anchor_fixture()
    instructions = current.instructions
    tools = current.tools
    if drift in {"instructions", "both"}:
        instructions = "新约束" * 1000
    if drift in {"tools", "both"}:
        tools = (tools[0].model_copy(update={"description": "新的工具约束" * 1000}),)
    current = current.model_copy(update={"instructions": instructions, "tools": tools})
    guarded = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
    if drift == "both":
        provider._completed_input = None
        assert guarded == provider.estimate_input_tokens(
            current, prefix_items=len(previous.history)
        )
    elif drift == "instructions":
        assert (
            guarded >= len(encode_json({"role": "system", "content": instructions}).encode()) + 512
        )
    else:
        body, _ = build_request(current, provider.config)
        assert guarded >= len(encode_json(body["tools"]).encode()) + 512


def test_anchor_stores_only_hashes_and_is_independent_of_mutable_request_fields():
    provider, previous, current = anchor_fixture()
    assert previous.instructions not in repr(provider._completed_input)
    current.tools[0].input_schema["description"] = "mutable drift" * 1000
    guarded = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
    body, _ = build_request(current, provider.config)
    assert guarded >= len(encode_json(body["tools"]).encode()) + 512


@pytest.mark.parametrize("terminal", ["length", "content_filter", "malformed"])
async def test_non_success_stream_cannot_publish_wrapper_proof(terminal):
    from tests.models.wire import chunk, frame

    frames = text_frames()
    if terminal == "malformed":
        frames = [b"data: invalid-json\n\n"]
    else:
        frames[2] = frame(chunk(finish=terminal))
    async with OpenAIChatProvider(
        OpenAIChatConfig(model="test-model"),
        api_key="offline-fixture",
        transport=httpx.MockTransport(lambda _: response(WireStream(frames))),
    ) as provider:
        request = model_request()
        assert [event async for event in provider.stream(request, CancelToken())]
        assert provider._completed_input is None


def pure_provider(**config):
    provider = object.__new__(OpenAIChatProvider)
    provider.config = OpenAIChatConfig(model="test-model", **config)
    return provider


@pytest.mark.parametrize("choice", ["auto", "required", "forced"])
@pytest.mark.parametrize("parallel", [True, False])
def test_wire_utf8_includes_alias_literals_closed_group_and_overhead(choice, parallel):
    request = model_request(with_tools=True)
    schema = {
        "type": "object",
        "title": "removed-title" * 100,
        "properties": {
            "title": {"type": "string", "title": "removed-child", "const": '字面量"\\\n🙂'}
        },
        "default": {"title": "literal-title"},
    }
    descriptor = request.tools[0].model_copy(update={"input_schema": schema})
    first, second = call(), call()
    request = request.model_copy(
        update={
            "tools": (descriptor,),
            "instructions": '系统"\\\n🙂',
            "remaining_tokens": 900,
            "tool_choice": "auto" if choice == "auto" else "required",
            "forced_tool": descriptor.name if choice == "forced" else None,
            "history": (
                *request.history,
                item(first),
                item(second),
                item(
                    ToolResultContent(
                        call_id=first.call_id,
                        outcome="succeeded",
                        output={"value": '引号"\\🙂' * 1000},
                    )
                ),
                item(ToolResultContent(call_id=second.call_id, outcome="succeeded", output=2)),
            ),
        }
    )
    provider = pure_provider(capabilities=ChatCapabilities(parallel_tool_calls=parallel))
    body, _ = build_request(request, provider.config)
    wire = encode_json(body).encode("utf-8")
    before = request.model_dump_json()
    assert isinstance(provider, ModelInputTokenEstimator)
    assert provider.estimate_input_tokens(request) == len(wire) + 512
    assert len(wire) > len(encode_json(body))
    function = body["tools"][0]["function"]
    assert function["name"] == tool_alias(descriptor.name)
    assert function["description"] == descriptor.name + ": " + descriptor.description
    assert "title" not in function["parameters"]
    assert function["parameters"]["default"]["title"] == "literal-title"
    assert function["parameters"]["properties"]["title"]["const"] == '字面量"\\\n🙂'
    assert body["parallel_tool_calls"] is parallel
    assert request.model_dump_json() == before
    assert INCREMENTAL_REQUEST_OVERHEAD_TOKENS == 512
    prefix = request.model_copy(
        update={
            "history": request.history[:1],
            "tools": (),
            "instructions": None,
            "tool_choice": "auto",
            "forced_tool": None,
        }
    )
    prefix_body, _ = build_request(prefix, provider.config)
    assert provider.estimate_input_tokens(request, prefix_items=1) == (
        len(wire) - len(encode_json(prefix_body).encode("utf-8")) + 512
    )


def test_estimator_never_reads_credentials_client_or_writes(monkeypatch):
    provider = pure_provider()
    forbidden = Mock(side_effect=AssertionError("no I/O"))
    monkeypatch.setattr(openai_chat, "read_key", forbidden)
    monkeypatch.setattr(openai_chat, "validate_key", forbidden)
    provider._client = forbidden
    provider._closed = True
    assert provider.estimate_input_tokens(model_request()) > 0
    forbidden.assert_not_called()


def test_unadvertised_effect_and_output_metadata_not_counted():
    request = model_request(with_tools=True)
    provider = pure_provider()
    changed = request.tools[0].model_copy(
        update={
            "public_output_schema": {"type": "object", "description": "x" * 40_000},
            "requires_approval": True,
            "supports_reconciliation": True,
        }
    )
    assert provider.estimate_input_tokens(request) == provider.estimate_input_tokens(
        request.model_copy(update={"tools": (changed,)})
    )


def test_invalid_unclosed_history_fails_without_http():
    provider = pure_provider()
    request = model_request(with_tools=True)
    with pytest.raises(InvalidModelRequest):
        provider.estimate_input_tokens(
            request.model_copy(
                update={
                    "history": (*request.history, item(call())),
                }
            )
        )


@pytest.mark.parametrize("prefix_items", [-1, True, None, 1.0, 999])
def test_invalid_prefix_never_produces_increment_bound(prefix_items):
    provider = pure_provider()
    with pytest.raises(InvalidModelRequest):
        provider.estimate_input_tokens(model_request(), prefix_items=prefix_items)


def test_unclosed_prefix_cannot_be_subtracted():
    provider = pure_provider()
    request = model_request(with_tools=True)
    invocation = call()
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                item(invocation),
                item(ToolResultContent(call_id=invocation.call_id, outcome="succeeded", output=1)),
            )
        }
    )
    with pytest.raises(InvalidModelRequest):
        provider.estimate_input_tokens(request, prefix_items=2)


@pytest.mark.parametrize(
    "thinking, thinking_budget", [(None, None), (True, None), (False, None), (True, 512)]
)
async def test_bound_covers_actual_sdk_body_with_extra_body_merge(thinking, thinking_budget):
    sent = []

    def handle(request):
        sent.append(request.content)
        return response(WireStream(text_frames()))

    async with OpenAIChatProvider(
        OpenAIChatConfig(
            model="test-model", enable_thinking=thinking, thinking_budget=thinking_budget
        ),
        api_key="offline-fixture",
        transport=httpx.MockTransport(handle),
    ) as provider:
        request = model_request()
        bound = provider.estimate_input_tokens(request)
        assert sent == []
        events = [event async for event in provider.stream(request, CancelToken())]
        assert events
    assert len(sent) == 1
    assert bound >= len(sent[0]) + 512
    assert b'"extra_body"' not in sent[0]
