"""当前目录反馈、原生历史边界及真实 SDK Mock 输入计费契约。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

import httpx
import httpx2
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.model_input_budget import INCREMENTAL_REQUEST_OVERHEAD_TOKENS
from harnessix.agent.models import TextContent, ToolCallRejectionContent, ToolResultContent, Usage
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS
from harnessix.agent.tool_rejections import rejection_result
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_input_anchor import CompletedChatInput
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models._history import encode_json, messages_for, tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelInputTokenEstimator,
    ModelRequest,
    ResponseCompleted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.context.test_compaction_wire_prepay import (
    input_budget,
    last_requests,
    plan,
)
from tests.context.test_compaction_wire_prepay import (
    wire_host as wire_host,
)
from tests.context.test_turn_budget_compaction import pressure_thread
from tests.contracts.provider import model_request
from tests.models import anthropic_wire, wire
from tests.models.test_chat_mapping import call, item

Kind = Literal["chat", "anthropic"]
CATALOG_PREFIX = "Current request tool catalog (wire aliases): "
SENTINEL = "harnessix_rejected_tool_v1"
PRIVATE_CANARY = "PRIVATE_PROVIDER_ID_CANARY"
ARGUMENT_CANARY = "HISTORICAL_ARGUMENT_CANARY"


def build(kind: Kind, request: ModelRequest) -> tuple[dict[str, Any], dict[str, str]]:
    if kind == "chat":
        return chat_request(request, OpenAIChatConfig(model="test-model"))
    return anthropic_request(request, AnthropicConfig(model="test-model"))


def system_text(kind: Kind, body: dict[str, Any]) -> str:
    if kind == "chat":
        return "\n\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
    return body.get("system", "")


def catalog_note(kind: Kind, body: dict[str, Any]) -> str:
    return next(
        note for note in system_text(kind, body).split("\n\n") if note.startswith(CATALOG_PREFIX)
    )


def catalog_aliases(note: str) -> list[str]:
    return json.loads(note.splitlines()[0].removeprefix(CATALOG_PREFIX))


def history_request(history: str = "mixed") -> ModelRequest:
    request = model_request(with_tools=True)
    invoked = call().model_copy(
        update={
            "tool": "legacy.read",
            "provider_call_id": PRIVATE_CANARY,
            "arguments": {"path": ARGUMENT_CANARY},
        }
    )
    rejected = ToolCallRejectionContent(
        call_id=uuid4(), provider_call_id=PRIVATE_CANARY, model_step=1
    )
    calls = (
        (invoked,)
        if history == "known"
        else (rejected,)
        if history == "rejected"
        else (invoked, rejected)
    )
    results = tuple(
        rejection_result(content.call_id)
        if isinstance(content, ToolCallRejectionContent)
        else ToolResultContent(call_id=content.call_id, outcome="succeeded", output="旧证据")
        for content in reversed(calls)
    )
    return request.model_copy(
        update={
            "history": (*request.history, *(item(c) for c in calls), *(item(r) for r in results)),
            "tools": tuple(
                request.tools[0].model_copy(
                    update={
                        "name": name,
                        "public_output_schema": {"type": "string", "description": PRIVATE_CANARY},
                    }
                )
                for name in ("read_file", "namespace.读取", SENTINEL)
            ),
        }
    )


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
@pytest.mark.parametrize("history", ["known", "rejected", "mixed"])
@pytest.mark.parametrize("empty", [False, True])
def test_catalog_uses_only_current_wire_aliases_without_mutating_input(kind, history, empty):
    request = history_request(history)
    if empty:
        request = request.model_copy(update={"tools": ()})
    before = request.model_dump_json()
    body, names = build(kind, request)
    note = catalog_note(kind, body)
    advertised = [
        t["function"]["name"] if kind == "chat" else t["name"] for t in body.get("tools", [])
    ]
    assert catalog_aliases(note) == advertised == list(names)
    assert "historical tool calls do not grant current permission" in note
    assert f"{SENTINEL} is a history-only rejection sentinel, not a callable tool" in note
    assert SENTINEL not in advertised
    assert "Tool choice: auto" in note and "end honestly" in note
    assert all(
        value not in note for value in ("legacy.read", tool_alias("legacy.read"), ARGUMENT_CANARY)
    )
    assert PRIVATE_CANARY not in encode_json(body)
    assert request.model_dump_json() == before
    if empty:
        assert "No tools are callable" in note and "tool_choice" not in body


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
@pytest.mark.parametrize("choice", ["required", "forced"])
def test_required_and_forced_feedback_matches_wire_without_optional_escape(kind, choice):
    request = history_request().model_copy(update={"tool_choice": "required"})
    if choice == "forced":
        request = request.model_copy(update={"forced_tool": request.tools[1].name})
    before = request.model_dump_json()
    body, _ = build(kind, request)
    note = catalog_note(kind, body)
    assert f"Tool choice: {choice}" in note and "must call" in note
    assert "text-only completion does not satisfy" in note
    assert "end honestly" not in note and "optional" not in note
    if choice == "forced":
        alias = tool_alias(request.forced_tool)
        assert encode_json(alias) in note and request.forced_tool not in note
        assert body["tool_choice"] == (
            {"type": "function", "function": {"name": alias}}
            if kind == "chat"
            else {"type": "tool", "disable_parallel_tool_use": False, "name": alias}
        )
    else:
        assert body["tool_choice"] == (
            "required" if kind == "chat" else {"type": "any", "disable_parallel_tool_use": False}
        )
    assert request.model_dump_json() == before


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
def test_catalog_changes_only_feedback_and_advertisement_not_native_history(kind):
    request = history_request()
    changed = request.model_copy(
        update={"tools": (request.tools[0].model_copy(update={"name": "new_tool"}),)}
    )
    old_body, _ = build(kind, request)
    new_body, names = build(kind, changed)
    assert catalog_aliases(catalog_note(kind, new_body)) == ["new_tool"] == list(names)
    assert catalog_note(kind, old_body) != catalog_note(kind, new_body)
    old_messages = [m for m in old_body["messages"] if m["role"] != "system"]
    new_messages = [m for m in new_body["messages"] if m["role"] != "system"]
    assert old_messages == new_messages
    native = messages_for(request)
    uses = native[1]["tool_calls"]
    assert [use["function"]["name"] for use in uses] == [tool_alias("legacy.read"), SENTINEL]
    assert uses[0]["function"]["arguments"] == encode_json({"path": ARGUMENT_CANARY})
    assert uses[1]["function"]["arguments"] == "{}"
    assert [m["tool_call_id"] for m in native[2:]] == [use["id"] for use in reversed(uses)]
    result = json.loads(native[2]["content"])
    assert result == rejection_result(request.history[2].content.call_id).model_dump(
        mode="json", include={"outcome", "output", "error", "diff_artifact"}
    )


# 本改动前从两个 mapper 的精确 UTF-8 wire 捕获，覆盖工具、指令和预算反馈。
LEGACY_WIRE_SHA256 = {
    ("chat", "plain"): "b6e9eae6f7ee9b1746abe33b694539870888fddd4e9e6bcb205fd5a9e8811a80",
    ("chat", "tools"): "d6c81f73d2c00dc4a911ea142878af9aca7db7645637d6267c917bcee3ca4de8",
    ("chat", "required"): "d662ce44e45e99a49583f185dd333c77d503bc2c1d62e19b2423790e9526d627",
    ("chat", "forced"): "6668a6f0be5dcaeb5b649b1a2e47234530d5a86d6207981270525c0b42083a91",
    ("anthropic", "plain"): "e9398497c60ff0cb2e1f31194203b15ebd931ce7fd21a45a2df126312adb0c27",
    ("anthropic", "tools"): "af944740efaef32a2eb74089d709a50522cb38863583630357d88d2469599167",
    ("anthropic", "required"): "7664ba240e767c535b970e89a46a047427529d1848a4b8c37f14a0b92a8fb35a",
    ("anthropic", "forced"): "4239cb6394d605a7c65777f400258186690255db4952e940a50e6fe7986dcb3f",
}


@pytest.mark.parametrize("kind, mode", LEGACY_WIRE_SHA256)
def test_first_request_without_tool_history_keeps_legacy_wire_bytes(kind, mode):
    request = model_request(with_tools=mode != "plain")
    if mode in {"required", "forced"}:
        request = request.model_copy(
            update={
                "instructions": "系统约束",
                "consumed_usage": Usage(input_tokens=37, output_tokens=4),
                "remaining_calls": 2,
                "input_tokens_upper_bound": 4096,
                "tool_choice": "required",
                "forced_tool": request.tools[0].name if mode == "forced" else None,
            }
        )
    body, _ = build(kind, request)
    assert CATALOG_PREFIX not in system_text(kind, body)
    assert hashlib.sha256(encode_json(body).encode()).hexdigest() == LEGACY_WIRE_SHA256[kind, mode]


def history_prefix(request: ModelRequest) -> ModelRequest:
    return request.model_copy(
        update={
            "tools": (),
            "instructions": None,
            "consumed_usage": None,
            "tool_choice": "auto",
            "forced_tool": None,
        }
    )


@pytest.mark.parametrize("anchored", [False, True])
@pytest.mark.parametrize("drift", ["same", "catalog", "empty", "instructions", "choice"])
def test_real_tool_history_prefix_never_subtracts_a_fictitious_catalog_or_discounts_note(
    anchored, drift
):
    provider = object.__new__(OpenAIChatProvider)
    provider.config = OpenAIChatConfig(model="test-model")
    previous = history_request().model_copy(update={"instructions": "约束" * 100})
    previous_body, _ = chat_request(previous, provider.config)
    prefix_body, _ = chat_request(
        history_prefix(previous), provider.config, include_catalog_note=False
    )
    anchor = CompletedChatInput.capture(previous, previous_body, provider.config)
    assert (
        anchor.history_messages_sha256
        == hashlib.sha256(encode_json(prefix_body["messages"]).encode()).hexdigest()
    )
    provider._completed_input = anchor if anchored else None
    current = previous.model_copy(
        update={
            "step": 2,
            "history": (
                *previous.history,
                item(TextContent(kind="assistant_message", text='新证据"\\🙂')),
            ),
        }
    )
    if drift == "catalog":
        current = current.model_copy(
            update={"tools": (current.tools[0].model_copy(update={"name": "new_tool"}),)}
        )
    elif drift == "empty":
        current = current.model_copy(update={"tools": ()})
    elif drift == "instructions":
        current = current.model_copy(update={"instructions": "新约束" * 100})
    elif drift == "choice":
        current = current.model_copy(
            update={"tool_choice": "required", "forced_tool": current.tools[1].name}
        )
    body, _ = chat_request(current, provider.config)
    without_note, _ = chat_request(current, provider.config, include_catalog_note=False)
    redundant = 0
    if anchored:
        if drift not in {"catalog", "empty"}:
            redundant += len(encode_json(body["tools"]).encode())
        if drift != "instructions":
            redundant += len(
                encode_json({"role": "system", "content": current.instructions}).encode()
            )
    overhead = INCREMENTAL_REQUEST_OVERHEAD_TOKENS
    expected = (
        len(encode_json(body).encode())
        - len(encode_json(prefix_body).encode())
        - redundant
        + overhead
    )
    assert provider.estimate_input_tokens(current, prefix_items=len(previous.history)) == expected
    note_charge = len(encode_json(body).encode()) - len(encode_json(without_note).encode())
    assert note_charge > 0 and expected >= note_charge + overhead
    assert provider.estimate_input_tokens(current) == len(encode_json(body).encode()) + overhead
    fake_prefix, _ = chat_request(history_prefix(previous), provider.config)
    assert catalog_aliases(catalog_note("chat", fake_prefix)) == []
    assert len(encode_json(fake_prefix).encode()) > len(encode_json(prefix_body).encode())


@pytest.mark.parametrize("drift", ["same", "catalog", "empty"])
async def test_completed_sdk_tool_history_prefix_bound_covers_actual_next_wire(drift):
    sent = []

    def handle(request):
        sent.append(request.content)
        return wire.response(wire.WireStream(wire.text_frames()))

    async with OpenAIChatProvider(
        OpenAIChatConfig(model="test-model"),
        api_key="offline-fixture",
        transport=httpx.MockTransport(handle),
    ) as provider:
        previous = history_request().model_copy(update={"instructions": "约束" * 100})
        current = previous.model_copy(
            update={
                "step": 2,
                "history": (
                    *previous.history,
                    item(TextContent(kind="assistant_message", text="你好")),
                    item(TextContent(kind="user_message", text="继续")),
                ),
                "tools": previous.tools
                if drift == "same"
                else ()
                if drift == "empty"
                else (previous.tools[0],),
            }
        )
        unanchored = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
        assert [event async for event in provider.stream(previous, CancelToken())]
        anchored = provider.estimate_input_tokens(current, prefix_items=len(previous.history))
        assert provider._completed_input is not None
        prefix, _ = chat_request(
            history_prefix(previous), provider.config, include_catalog_note=False
        )
        body, _ = chat_request(current, provider.config)
        redundant = len(encode_json({"role": "system", "content": current.instructions}).encode())
        if drift == "same":
            redundant += len(encode_json(body["tools"]).encode())
        assert anchored == unanchored - redundant
        assert [event async for event in provider.stream(current, CancelToken())]
        assert anchored >= len(sent[1]) - len(encode_json(prefix).encode()) - redundant + 512
        note_bytes = len(
            encode_json({"role": "system", "content": catalog_note("chat", body)}).encode()
        )
        assert anchored >= note_bytes + 512
        assert json.loads(sent[1]) == body
    assert len(sent) == 2


async def test_compaction_full_wire_prepayment_keeps_catalog_feedback_billed(wire_host):
    source = pressure_thread()
    planned, context, selection = await plan(wire_host, source)
    assert planned is not None
    summary, original, successor = last_requests(wire_host)
    costs = input_budget(wire_host, source, context, selection).estimate_costs(
        planned, wire_host.host._compaction, SUMMARY_INSTRUCTIONS
    )
    # 只比较原请求与后继请求；两者都有真实工具历史，必须完整预付当前说明。
    for request, paid in zip((original, successor), costs[1:], strict=True):
        body, _ = chat_request(request, wire_host.main.config)
        without_note, _ = chat_request(request, wire_host.main.config, include_catalog_note=False)
        assert catalog_note("chat", body)
        assert paid == len(encode_json(body).encode()) + 512
        assert paid > len(encode_json(without_note).encode()) + 512
    assert all(
        call.kwargs.get("prefix_items", 0) == 0
        for call in wire_host.main.estimate_input_tokens.call_args_list
    )
    assert CATALOG_PREFIX not in system_text(
        "chat", chat_request(summary, wire_host.summary.config)[0]
    )


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
@pytest.mark.parametrize("choice", ["auto", "required", "forced"])
async def test_real_sdk_mock_sends_full_catalog_feedback_and_charges_full_wire(kind, choice):
    request = history_request().model_copy(update={"tools": model_request(with_tools=True).tools})
    if choice != "auto":
        request = request.model_copy(
            update={
                "tool_choice": "required",
                "forced_tool": request.tools[0].name if choice == "forced" else None,
            }
        )
    before = request.model_dump_json()
    sent = []
    fixtures = wire if kind == "chat" else anthropic_wire

    def handle(http_request):
        sent.append(http_request.content)
        frames = fixtures.text_frames() if choice == "auto" else fixtures.tool_frames()
        return fixtures.response(fixtures.WireStream(frames))

    provider = (
        OpenAIChatProvider(
            OpenAIChatConfig(model="test-model", enable_thinking=True, thinking_budget=128),
            api_key="offline-fixture",
            transport=httpx.MockTransport(handle),
        )
        if kind == "chat"
        else AnthropicProvider(
            AnthropicConfig(model="test-model"),
            api_key="offline-fixture",
            transport=httpx2.MockTransport(handle),
        )
    )
    async with provider:
        if kind == "chat":
            body, _ = chat_request(request, provider.config)
            bound = provider.estimate_input_tokens(request)
            assert bound == len(encode_json(body).encode()) + 512
        else:
            body, _ = anthropic_request(request, provider.config)
            assert not isinstance(provider, ModelInputTokenEstimator)
        assert sent == []
        events = [event async for event in provider.stream(request, CancelToken())]
        assert isinstance(events[-1], ResponseCompleted)
        assert any(isinstance(event, ToolCallCompleted) for event in events) == (choice != "auto")
    assert len(sent) == 1
    actual = json.loads(sent[0])
    if kind == "chat":
        extra = body.pop("extra_body")
        body.update(extra)
        assert bound >= len(sent[0]) + 512
    assert actual == body
    assert catalog_note(kind, actual) == catalog_note(kind, body)
    assert request.model_dump_json() == before


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
async def test_unknown_sdk_name_and_arguments_never_enter_rejection_feedback(kind):
    fixtures = wire if kind == "chat" else anthropic_wire
    frames = fixtures.tool_frames(arguments='{"path":"UNKNOWN_ARGUMENT_CANARY"}')
    frames = [
        frame.replace(tool_alias("test.read").encode(), b"UNKNOWN_NAME_CANARY") for frame in frames
    ]
    provider = (
        OpenAIChatProvider(
            OpenAIChatConfig(model="test-model"),
            api_key="offline-fixture",
            transport=httpx.MockTransport(lambda _: fixtures.response(fixtures.WireStream(frames))),
        )
        if kind == "chat"
        else AnthropicProvider(
            AnthropicConfig(model="test-model"),
            api_key="offline-fixture",
            transport=httpx2.MockTransport(
                lambda _: fixtures.response(fixtures.WireStream(frames))
            ),
        )
    )
    request = model_request(with_tools=True)
    async with provider:
        events = [event async for event in provider.stream(request, CancelToken())]
    rejected = next(event for event in events if isinstance(event, ToolCallRejected))
    assert not any(isinstance(event, ToolCallCompleted) for event in events)
    call_id = uuid4()
    current = request.model_copy(
        update={
            "history": (
                *request.history,
                item(
                    ToolCallRejectionContent(
                        call_id=call_id, provider_call_id=rejected.call_id, model_step=1
                    )
                ),
                item(rejection_result(call_id)),
            )
        }
    )
    body, _ = build(kind, current)
    assert catalog_aliases(catalog_note(kind, body)) == [tool_alias("test.read")]
    assert "UNKNOWN_NAME_CANARY" not in encode_json(body)
    assert "UNKNOWN_ARGUMENT_CANARY" not in encode_json(body)
