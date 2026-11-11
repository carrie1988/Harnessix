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
from harnessix.agent.models import (
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.models import _anthropic_mapping, _chat_mapping
from harnessix.models._history import (
    InvalidModelRequest,
    messages_for,
    tool_alias,
    tool_aliases,
)
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.contracts.provider import model_request
from tests.models import anthropic_wire, wire
from tests.models.test_chat_mapping import call, item

ASCII_STEM_CHARACTERS = string.ascii_letters + string.digits + "_-"


def digest8(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def legacy_hashed_alias(name: str) -> str:
    """ADR 0136之前的别名：hx_加可读片段前27字符加32位摘要。"""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "tool"
    return f"hx_{stem[:27]}_{hashlib.sha256(name.encode()).hexdigest()[:32]}"


@pytest.mark.parametrize(
    ("name", "alias"),
    [
        ("", "tool"),
        ("read_file", "read_file"),
        ("run-profile", "run-profile"),
        ("AZ_az09-", "AZ_az09-"),
        # 评测中的Shell工具：56字符、以32位十六进制结尾的旧别名被抄错45次。
        ("exec_command.testbed", "exec_command_testbed"),
        ("namespace.read_file", "namespace_read_file"),
        ("test.read", "test_read"),
        ("a b/c:d\t\n", "a_b_c_d__"),
        ("工具/读取", "_____"),
        ("é", "_"),
        ("e\u0301", "e_"),
        ("Ａ１", "__"),
        ("😀", "_"),
        ("café.Read", "caf__Read"),
        ("x" * 64, "x" * 64),
        ("x" * 65, "x" * 64),
        ("prefix." * 100, ("prefix_" * 10)[:64]),
    ],
)
def test_alias_only_replaces_characters_the_wire_does_not_accept(name: str, alias: str) -> None:
    assert tool_alias(name) == alias
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
    assert tool_alias(name) == alias


@pytest.mark.parametrize(
    "code", [code for code in range(128) if chr(code) not in ASCII_STEM_CHARACTERS]
)
def test_each_disallowed_ascii_character_is_replaced_once(code: int) -> None:
    assert tool_alias("a" + chr(code) + "Z") == "a_Z"


def test_each_allowed_ascii_character_is_preserved_without_case_repair() -> None:
    for character in ASCII_STEM_CHARACTERS:
        name = "a" + character + "Z"
        assert tool_alias(name) == name


def test_catalog_without_collisions_uses_the_readable_aliases_unchanged() -> None:
    names = ("read_file", "exec_command.testbed", "run_profile.unit", "Read_File", "READ_FILE")
    assert tool_aliases(names) == {
        "read_file": "read_file",
        "exec_command.testbed": "exec_command_testbed",
        "run_profile.unit": "run_profile_unit",
        "Read_File": "Read_File",
        "READ_FILE": "READ_FILE",
    }


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        # 合法原名保留自己的名字，另一个加后缀。
        (("test.read", "test_read"), {"test_read": "test_read"}),
        (("a.b", "a/b"), {}),
        (("工具", "读取"), {}),
        (("é", "è", "plain"), {"plain": "plain"}),
        (("x" * 64 + "one", "x" * 64 + "two"), {}),
        (("x" * 64, "x" * 65), {"x" * 64: "x" * 64}),
        (("?", "!"), {}),
        (("\x00", "\x01"), {}),
    ],
)
def test_names_that_collide_after_replacement_get_a_short_exact_suffix(
    names: tuple[str, ...], expected: dict[str, str]
) -> None:
    aliases = tool_aliases(names)
    assert len(set(aliases.values())) == len(names)
    for name in names:
        alias = aliases[name]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
        if name in expected:
            assert alias == expected[name]
        else:
            assert alias == f"{tool_alias(name)[:55]}_{digest8(name)}"
    # 映射只由名字集合决定：换顺序、重复计算结果相同。
    assert tool_aliases(reversed(names)) == aliases == tool_aliases(names)


def test_suffix_is_only_spent_on_the_names_that_collide() -> None:
    aliases = tool_aliases(("a.b", "a_b", "exec_command.testbed"))
    assert aliases == {
        "a.b": f"a_b_{digest8('a.b')}",
        "a_b": "a_b",
        "exec_command.testbed": "exec_command_testbed",
    }


@pytest.mark.parametrize("name", ["harnessix_rejected_tool_v1", "harnessix.rejected.tool.v1"])
def test_rejection_history_marker_is_never_advertised_as_a_tool(name: str) -> None:
    marker = "harnessix_rejected_tool_v1"
    assert tool_alias(name) == f"{marker}_{digest8(name)}"
    assert tool_aliases((name,)) == {name: f"{marker}_{digest8(name)}"}


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
    aliases = tool_aliases(names)
    expected = [aliases[name] for name in names]
    assert len(set(expected)) == len(names)
    assert [aliases[name] for name in ("read_file", "test_read", "Read_File")] == [
        "read_file",
        "test_read",
        "Read_File",
    ]
    history = messages_for(request)
    assert [entry["function"]["name"] for entry in history[1]["tool_calls"]] == expected
    body, reverse_names = build_for(kind, request)
    assert reverse_names == dict(zip(expected, names, strict=True))
    if kind == "openai":
        definitions = [entry["function"] for entry in body["tools"]]
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"].startswith("Current request tool catalog")
        projected = [entry["function"]["name"] for entry in body["messages"][2]["tool_calls"]]
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
@pytest.mark.parametrize(
    "names",
    [
        # 加后缀后的别名恰好是另一个已注册的合法原名：无法一一对应，整体拒绝。
        ("test.read", "test_read", f"test_read_{digest8('test.read')}"),
        ("test.read", "test.read"),
    ],
    ids=["suffixed-alias-is-registered", "duplicate-name"],
)
def test_catalog_without_a_one_to_one_alias_mapping_is_rejected(
    kind: str, names: tuple[str, ...]
) -> None:
    request = model_request(with_tools=True)
    definitions = tuple(request.tools[0].model_copy(update={"name": name}) for name in names)
    with pytest.raises(InvalidModelRequest, match="工具名称或输入 Schema 无效"):
        build_for(kind, request.model_copy(update={"tools": definitions}))


def test_forced_choice_and_catalog_note_use_the_catalog_alias_of_a_colliding_name() -> None:
    request = model_request(with_tools=True)
    definitions = tuple(
        request.tools[0].model_copy(update={"name": name}) for name in ("test.read", "test_read")
    )
    invoked = call().model_copy(update={"tool": "test.read"})
    request = request.model_copy(
        update={
            "tools": definitions,
            "tool_choice": "required",
            "forced_tool": "test.read",
            "history": (
                *request.history,
                item(invoked),
                item(ToolResultContent(call_id=invoked.call_id, outcome="succeeded")),
            ),
        }
    )
    suffixed = f"test_read_{digest8('test.read')}"
    chat, chat_names = build_for("openai", request)
    assert chat_names == {suffixed: "test.read", "test_read": "test_read"}
    assert chat["tool_choice"] == {"type": "function", "function": {"name": suffixed}}
    assert f'Tool choice: forced ("{suffixed}")' in chat["messages"][0]["content"]
    assert chat["messages"][2]["tool_calls"][0]["function"]["name"] == suffixed
    anthropic, anthropic_names = build_for("anthropic", request)
    assert anthropic_names == chat_names
    assert anthropic["tool_choice"]["name"] == suffixed
    assert f'Tool choice: forced ("{suffixed}")' in anthropic["system"]


def test_history_call_to_a_tool_outside_the_catalog_never_reads_as_a_current_tool() -> None:
    request = model_request(with_tools=True)
    calls = tuple(call().model_copy(update={"tool": name}) for name in ("old.read", "gone.tool"))
    request = request.model_copy(
        update={
            "tools": (request.tools[0].model_copy(update={"name": "old_read"}),),
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
    projected = [entry["function"]["name"] for entry in messages_for(request)[1]["tool_calls"]]
    # old.read替换后与当前目录的old_read相同，加后缀；gone.tool不冲突，保持可读。
    assert projected == [f"old_read_{digest8('old.read')}", "gone_tool"]
    _, names = build_for("openai", request)
    assert names == {"old_read": "old_read"}


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
        body, reverse_names = build_for(kind, request)
        assert reverse_names == {"test_read": "test.read"}
        # 会话只保存原名：旧版本写入的历史在新版本下直接以可读别名重放，线上不再出现旧摘要别名。
        assert legacy_hashed_alias("test.read") not in repr(body)
    replayed = messages_for(request)[1]["tool_calls"]
    assert [entry["function"]["name"] for entry in replayed] == ["test_read"]
    assert original_calls[0].tool == "test.read"
    assert await reopened.get_thread(thread.thread_id) == persisted
    assert [event.model_dump_json() for event in await reopened.events(thread.thread_id)] == before


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("logical_name", ["test.read", "read_file", "exec_command.testbed"])
@pytest.mark.parametrize(
    "identity",
    [
        "current",
        "unknown",
        "legacy_hash",
        "previous_hashed_alias",
        "wrong_case",
        "not_in_directory",
        "raw_name",
    ],
)
async def test_actual_sdk_stream_accepts_only_current_exact_directory_alias(
    kind: str, logical_name: str, identity: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    returned_name = {
        "current": tool_alias(logical_name),
        "unknown": "hx_unknown_fixture",
        "legacy_hash": "hx_" + hashlib.sha256(logical_name.encode()).hexdigest()[:60],
        "previous_hashed_alias": legacy_hashed_alias(logical_name),
        "wrong_case": tool_alias(logical_name).upper(),
        "not_in_directory": tool_alias("test.other"),
        "raw_name": logical_name,
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
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={"tools": (request.tools[0].model_copy(update={"name": logical_name}),)}
    )
    async with provider:
        events = [event async for event in provider.stream(request, CancelToken())]
    assert stream.closed
    if returned_name == tool_alias(logical_name):
        calls = [event for event in events if isinstance(event, ToolCallCompleted)]
        assert len(calls) == 1 and calls[0].tool == logical_name and calls[0].arguments == {}
        assert isinstance(events[-1], ResponseCompleted)
        assert not any(isinstance(event, ToolCallRejected) for event in events)
    else:
        assert [
            event for event in events if isinstance(event, ToolCallCompleted | ToolCallRejected)
        ] == [
            ToolCallRejected(
                call_id="wire-call-0" if kind == "openai" else "toolu_0", argument_chars=2
            )
        ]
        assert (
            isinstance(events[-1], ResponseCompleted) and events[-1].finish_reason == "tool_calls"
        )
        assert returned_name not in "".join(event.model_dump_json() for event in events)

    # 把实际 SDK 已归一化的事件交给原 Kernel，证明目录外原名也不具执行权。
    class RegisteredTools(RecordingTools):
        def definitions(self):
            return tuple(
                definition.model_copy(update={"name": logical_name})
                for definition in super().definitions()
            )

    tools = RegisteredTools()
    scripted = ScriptedProvider([events, answer()])
    store = SQLiteSessionStore(tmp_path / "alias-runtime.db")
    async with AgentRuntime(store, scripted, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "精确别名边界", request_id="alias-boundary")
    assert turn.status is TurnStatus.COMPLETED and turn.error is None and turn.model_steps == 2
    rejected = [
        entry for entry in turn.items if isinstance(entry.content, ToolCallRejectionContent)
    ]
    results = [
        entry.content for entry in turn.items if isinstance(entry.content, ToolResultContent)
    ]
    assert len(results) == 1
    if returned_name == tool_alias(logical_name):
        assert not rejected and len(tools.calls) == 1
        assert tools.calls[0].tool == logical_name and results[0].outcome == "succeeded"
    else:
        assert tools.calls == [] and len(rejected) == 1
        assert rejected[0].status is ItemStatus.COMPLETED and rejected[0].error is None
        assert not any(isinstance(entry.content, ToolCallContent) for entry in turn.items)
        assert results[0].call_id == rejected[0].content.call_id
        assert results[0].outcome == "failed" and results[0].error.code == "unknown_tool"
        assert results[0].output is None and not results[0].error.retryable
