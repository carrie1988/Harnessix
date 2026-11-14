"""说明随历史持久化时（ADR 0132），各Provider映射不再逐请求附加预算与目录说明。"""

from __future__ import annotations

import json

import pytest

from harnessix.agent.models import Item, TextContent, ToolResultContent, Usage
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import messages_for, tool_alias, tool_catalog_update
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ModelRequest
from tests.contracts.provider import model_request
from tests.models.test_chat_mapping import call, item

NOTE_MARKERS = ("Current request tool catalog", "Runtime budget", "[Harness note]")


def growing_requests(*, notes_in_history: bool) -> tuple[ModelRequest, ModelRequest]:
    """同一Turn相邻两步：都有工具调用历史与预算事实，旧映射会为每个请求各附两条说明。"""
    base = model_request(with_tools=True).model_copy(
        update={
            "instructions": "rules",
            "consumed_usage": Usage(input_tokens=1000, output_tokens=10),
            "tool_choice": "required",
            "notes_in_history": notes_in_history,
        }
    )
    first, second = call(), call()
    one = base.model_copy(
        update={
            "history": (
                *base.history,
                item(first),
                item(ToolResultContent(call_id=first.call_id, outcome="succeeded", output="a")),
            )
        }
    )
    two = one.model_copy(
        update={
            "step": 2,
            "consumed_usage": Usage(input_tokens=3000, output_tokens=30),
            "history": (
                *one.history,
                item(second),
                item(ToolResultContent(call_id=second.call_id, outcome="succeeded", output="b")),
            ),
        }
    )
    return one, two


def unmarked(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, list):
        blocks = [{k: v for k, v in block.items() if k != "cache_control"} for block in content]
        return {**message, "content": blocks}
    return message


@pytest.mark.parametrize("layout", [None, "explicit_tail", "stable_tail"])
def test_chat_requests_carry_no_per_request_notes_and_only_append(layout):
    config = OpenAIChatConfig(model="m", prompt_cache=layout)
    one, two = growing_requests(notes_in_history=True)
    first, _ = build_request(one, config)
    second, _ = build_request(two, config)
    for body in (first, second):
        assert not any(marker in json.dumps(body["messages"]) for marker in NOTE_MARKERS)
    before = [unmarked(message) for message in first["messages"]]
    after = [unmarked(message) for message in second["messages"]]
    assert after[: len(before)] == before and len(after) == len(before) + 2
    # 约束本身不变：required仍在线上，可执行集合仍由tools给出。
    assert first["tool_choice"] == "required" and first["tools"] == second["tools"]


@pytest.mark.parametrize("layout", [None, "explicit_tail", "stable_tail"])
def test_default_requests_keep_the_per_request_notes(layout):
    """开关默认关闭：旧宿主的请求不变，仍逐请求带目录与预算说明。"""
    one, _ = growing_requests(notes_in_history=False)
    body, _ = build_request(one, OpenAIChatConfig(model="m", prompt_cache=layout))
    rendered = json.dumps(body["messages"])
    assert "Current request tool catalog" in rendered and "Runtime budget" in rendered


def test_explicit_cache_marks_the_end_of_the_history_when_notes_are_in_history():
    one, _ = growing_requests(notes_in_history=True)
    body, _ = build_request(one, OpenAIChatConfig(model="m", prompt_cache="explicit_tail"))
    marked = [
        index
        for index, message in enumerate(body["messages"])
        if "cache_control" in json.dumps(message)
    ]
    # 指令一个标记；另一个落在历史最后一条文本消息上，其后没有易变内容。
    assert len(marked) == 2 and marked[0] == 0
    assert all(m["role"] != "system" for m in body["messages"][1:])


def test_anthropic_requests_carry_no_per_request_notes():
    config = AnthropicConfig(model="m")
    one, two = growing_requests(notes_in_history=True)
    first, _ = anthropic_request(one, config)
    second, _ = anthropic_request(two, config)
    assert first["system"] == second["system"] == "rules"
    assert second["messages"][: len(first["messages"])] == first["messages"]
    legacy, _ = anthropic_request(growing_requests(notes_in_history=False)[0], config)
    assert "Runtime budget" in legacy["system"] and "tool catalog" in legacy["system"]


def test_a_persisted_host_note_is_an_ordinary_user_message_after_the_tool_results():
    one, _ = growing_requests(notes_in_history=True)
    note = "[Harnessix host notice] Budget checkpoint: 50 of 100 model calls are left."
    noted = one.model_copy(
        update={"history": (*one.history, item(TextContent(kind="user_message", text=note)))}
    )
    body, _ = build_request(noted, OpenAIChatConfig(model="m", prompt_cache="stable_tail"))
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool", "user"]
    assert body["messages"][-1] == {"role": "user", "content": note}
    claude, _ = anthropic_request(noted, AnthropicConfig(model="m"))
    # Anthropic把说明并入工具结果所在的用户消息，历史仍以user结束。
    assert claude["messages"][-1]["role"] == "user"
    assert claude["messages"][-1]["content"][-1] == {"type": "text", "text": note}


def test_persisted_catalog_note_lists_wire_aliases_and_the_choice():
    text = tool_catalog_update(
        ["read_file", "namespace.读取"],
        tool_choice="required",
        forced_tool="read_file",
        notice=None,
    )
    first_line, rules = text.split("\n", 1)
    assert first_line == (
        "Callable tools from now on (wire aliases): "
        + json.dumps(["read_file", tool_alias("namespace.读取")], separators=(",", ":"))
    )
    assert "until a newer host notice replaces this list" in rules
    assert "harnessix_rejected_tool_v1 is a history-only rejection sentinel" in rules
    assert 'Tool choice: forced ("read_file")' in rules
    restricted = tool_catalog_update((), tool_choice="auto", forced_tool=None, notice="Why not.")
    assert "No tools are callable. Why the catalog is restricted now: Why not. " in restricted
    assert restricted.endswith("rather than inventing tools or results.")


def test_history_tool_calls_keep_the_models_argument_order_and_values():
    """历史里的助手消息应与模型当时的输出一致：参数不排序、不转义非ASCII，持久化往返后仍如此。

    参数以紧凑JSON重发（模型原文的空白不保留）；vLLM会把参数解析后交给对话模板重新渲染，
    因此键顺序与取值一致即可复用模型输出的缓存。
    """
    arguments = {"path": "src/é.py", "start_line": 10, "anchor": {"z": [1.5, True, None], "a": ""}}
    invoked = call().model_copy(update={"arguments": arguments})
    stored = Item.model_validate_json(item(invoked).model_dump_json())
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                stored,
                item(ToolResultContent(call_id=invoked.call_id, outcome="succeeded", output=1)),
            )
        }
    )
    assistant = next(m for m in messages_for(request) if m["role"] == "assistant")
    sent = assistant["tool_calls"][0]["function"]["arguments"]
    assert sent == '{"path":"src/é.py","start_line":10,"anchor":{"z":[1.5,true,null],"a":""}}'
    assert list(json.loads(sent)) == list(arguments)
