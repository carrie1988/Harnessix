"""输入估算只读复用出站 mapper，不创建第二套请求 JSON。"""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.model_input_budget import INCREMENTAL_REQUEST_OVERHEAD_TOKENS
from harnessix.agent.models import ToolResultContent
from harnessix.models import openai_chat
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import InvalidModelRequest, encode_json, tool_alias
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import ModelInputTokenEstimator
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.contracts.provider import model_request
from tests.models.test_chat_mapping import call, item
from tests.models.wire import WireStream, response, text_frames


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


@pytest.mark.parametrize("thinking", [None, True, False])
async def test_bound_covers_actual_sdk_body_with_extra_body_merge(thinking):
    sent = []

    def handle(request):
        sent.append(request.content)
        return response(WireStream(text_frames()))

    async with OpenAIChatProvider(
        OpenAIChatConfig(model="test-model", enable_thinking=thinking),
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
