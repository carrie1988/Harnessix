"""工具别名的可读身份、历史原名及严格反向目录契约。"""

from __future__ import annotations

import hashlib
import re
import string
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import TextContent, ToolCallContent, ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.models import _anthropic_mapping, _chat_mapping
from harnessix.models._history import InvalidModelRequest, messages_for, tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ResponseFailed,
    ToolCallCompleted,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.contracts.provider import model_request
from tests.models import anthropic_wire, wire
from tests.models.test_chat_mapping import call, item

ASCII_STEM_CHARACTERS = string.ascii_letters + string.digits + "_-"


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("", "tool"),
        ("read_file", "read_file"),
        ("run_profile", "run_profile"),
        ("namespace.read_file", "namespace_read_file"),
        ("run-profile", "run-profile"),
        ("AZ_az09-", "AZ_az09-"),
        ("test.read", "test_read"),
        ("a b/c:d\t\n", "a_b_c_d__"),
        ("工具/读取", "_____"),
        ("é", "_"),
        ("e\u0301", "e_"),
        ("Ａ１", "__"),
        ("😀", "_"),
        ("café.Read", "caf__Read"),
        ("a" * 27, "a" * 27),
        ("a" * 28, "a" * 27),
        ("prefix" * 100, ("prefix" * 5)[:27]),
    ],
)
def test_alias_has_readable_stem_and_exact_original_utf8_digest(name: str, stem: str) -> None:
    alias = tool_alias(name)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    assert alias == f"hx_{stem}_{digest}"
    assert alias.startswith("hx_") and alias.isascii()
    assert re.fullmatch(r"hx_[A-Za-z0-9_-]+_[0-9a-f]{32}", alias)
    assert len(alias.encode("ascii")) <= 63
    assert tool_alias(name) == alias


@pytest.mark.parametrize(
    "code", [code for code in range(128) if chr(code) not in ASCII_STEM_CHARACTERS]
)
def test_each_disallowed_ascii_character_is_replaced_once(code: int) -> None:
    name = "a" + chr(code) + "Z"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    assert tool_alias(name) == f"hx_a_Z_{digest}"


def test_each_allowed_ascii_character_is_preserved_without_case_repair() -> None:
    for character in ASCII_STEM_CHARACTERS:
        name = "a" + character + "Z"
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        assert tool_alias(name) == f"hx_{name}_{digest}"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("test.read", "test_read"),
        ("a" * 27 + "_one", "a" * 27 + "_two"),
        ("a" * 500 + "x", "a" * 500 + "y"),
        ("工具", "读取"),
        ("é", "è"),
        ("é", "e\u0301"),
        ("Read_File", "read_file"),
        ("READ_FILE", "read_file"),
        ("?", "!"),
        ("\x00", "\x01"),
    ],
)
def test_distinct_full_names_keep_distinct_digest_identity(first: str, second: str) -> None:
    assert tool_alias(first) != tool_alias(second)
    assert tool_alias(first)[-32:] == hashlib.sha256(first.encode("utf-8")).hexdigest()[:32]
    assert tool_alias(second)[-32:] == hashlib.sha256(second.encode("utf-8")).hexdigest()[:32]


@pytest.mark.parametrize("length", [1, 26, 27, 28, 256, 10000])
def test_ascii_byte_limit_is_63_even_for_long_original_names(length: int) -> None:
    alias = tool_alias("x" * length)
    assert alias.startswith("hx_" + "x" * min(length, 27) + "_")
    assert len(alias.encode("ascii")) == 36 + min(length, 27)


def build_for(kind: str, request: ModelRequest) -> tuple[dict[str, Any], dict[str, str]]:
    if kind == "openai":
        return _chat_mapping.build_request(request, OpenAIChatConfig(model="test-model"))
    return _anthropic_mapping.build_request(request, AnthropicConfig(model="test-model"))


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_tool_directory_and_history_share_alias_without_mutating_contracts(kind: str) -> None:
    names = (
        "read_file",
        "run_profile",
        "test.read",
        "test_read",
        "工具",
        "读取",
        "a" * 27 + "_one",
        "a" * 27 + "_two",
        "Read_File",
    )
    request = model_request(with_tools=True)
    calls = tuple(call().model_copy(update={"tool": name}) for name in names)
    request = request.model_copy(
        update={
            "tools": tuple(request.tools[0].model_copy(update={"name": name}) for name in names),
            "history": (
                *request.history,
                *(item(invoked) for invoked in calls),
                *(
                    item(ToolResultContent(call_id=invoked.call_id, outcome="succeeded"))
                    for invoked in calls
                ),
            ),
        }
    )
    before = request.model_dump_json()
    expected = [tool_alias(name) for name in names]
    history = messages_for(request)
    assert [entry["function"]["name"] for entry in history[1]["tool_calls"]] == expected
    body, reverse_names = build_for(kind, request)
    assert reverse_names == dict(zip(expected, names, strict=True))
    if kind == "openai":
        definitions = [entry["function"] for entry in body["tools"]]
        projected = [entry["function"]["name"] for entry in body["messages"][1]["tool_calls"]]
        schema_key = "parameters"
    else:
        definitions = body["tools"]
        projected = [entry["name"] for entry in body["messages"][1]["content"]]
        schema_key = "input_schema"
    assert [definition["name"] for definition in definitions] == projected == expected
    for definition, original in zip(definitions, request.tools, strict=True):
        assert definition["description"] == original.name + ": " + original.description
        assert definition[schema_key] == original.input_schema
    assert request.model_dump_json() == before
    assert [invoked.tool for invoked in calls] == list(names)


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_alias_collision_still_rejects_the_request(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={
            "tools": (
                request.tools[0],
                request.tools[0].model_copy(update={"name": "test.other"}),
            )
        }
    )
    mapping = _chat_mapping if kind == "openai" else _anthropic_mapping
    monkeypatch.setattr(mapping, "tool_alias", lambda _: "hx_collision_fixture")
    with pytest.raises(InvalidModelRequest, match="工具名称或输入 Schema 无效"):
        build_for(kind, request)


async def test_persisted_original_tool_name_survives_both_wire_projections(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "legacy-original-tool.db")
    tools = RecordingTools()
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "读取", request_id="persist-original-name")
    assert turn.status == TurnStatus.COMPLETED
    reopened = SQLiteSessionStore(store.path)
    persisted = await reopened.get_thread(thread.thread_id)
    original_calls = [
        entry.content
        for entry in persisted.turns[0].items
        if isinstance(entry.content, ToolCallContent)
    ]
    assert len(original_calls) == 1 and original_calls[0].tool == "test.read"
    before = [event.model_dump_json() for event in await reopened.events(thread.thread_id)]
    request = model_request(with_tools=True).model_copy(
        update={
            "history": (
                *persisted.turns[0].items,
                item(TextContent(kind="user_message", text="继续")),
            )
        }
    )
    for kind in ("openai", "anthropic"):
        _, reverse_names = build_for(kind, request)
        assert reverse_names[tool_alias("test.read")] == "test.read"
    assert original_calls[0].tool == "test.read"
    assert await reopened.get_thread(thread.thread_id) == persisted
    assert [event.model_dump_json() for event in await reopened.events(thread.thread_id)] == before


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "identity", ["current", "unknown", "legacy_hash", "wrong_case", "not_in_directory", "raw_name"]
)
async def test_actual_sdk_stream_accepts_only_current_exact_directory_alias(
    kind: str, identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    returned_name = {
        "current": tool_alias("test.read"),
        "unknown": "hx_unknown_fixture",
        "legacy_hash": "hx_" + hashlib.sha256(b"test.read").hexdigest()[:60],
        "wrong_case": tool_alias("test.read").upper(),
        "not_in_directory": tool_alias("test.other"),
        "raw_name": "test.read",
    }[identity]
    provider: OpenAIChatProvider | AnthropicProvider
    stream: wire.WireStream | anthropic_wire.WireStream
    if kind == "openai":
        invocation = wire.call()
        invocation["function"]["name"] = returned_name
        parts = wire.tool_frames()
        parts[0] = wire.frame(wire.chunk({"tool_calls": [invocation]}))
        stream = wire.WireStream(parts)
        provider = OpenAIChatProvider(
            OpenAIChatConfig(model="test-model"),
            api_key="offline-alias-fixture",
            transport=httpx.MockTransport(lambda _: wire.response(stream)),
        )
    else:
        parts = anthropic_wire.tool_frames()
        parts[1] = anthropic_wire.frame(
            "content_block_start",
            index=0,
            content_block={"type": "tool_use", "id": "toolu_0", "name": returned_name, "input": {}},
        )
        stream = anthropic_wire.WireStream(parts)
        provider = AnthropicProvider(
            AnthropicConfig(model="test-model"),
            api_key="offline-alias-fixture",
            transport=httpx2.MockTransport(lambda _: anthropic_wire.response(stream)),
        )
    async with provider:
        events = [
            event async for event in provider.stream(model_request(with_tools=True), CancelToken())
        ]
    assert stream.closed
    if identity == "current":
        calls = [event for event in events if isinstance(event, ToolCallCompleted)]
        assert len(calls) == 1 and calls[0].tool == "test.read" and calls[0].arguments == {}
        assert isinstance(events[-1], ResponseCompleted)
    else:
        assert events[-1] == ResponseFailed(code="invalid_provider_output")
        assert not any(isinstance(event, ToolCallCompleted | ResponseCompleted) for event in events)
