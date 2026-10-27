"""显式前缀缓存：稳定前缀在前并打标记、易变说明在末尾；默认映射与序列化不变。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.agent.models import ToolResultContent
from harnessix.models._chat_mapping import build_request
from harnessix.models.config import OpenAIChatConfig
from tests.contracts.provider import model_request
from tests.models.test_attempt_usage import adapter as adapter
from tests.models.test_attempt_usage import execute
from tests.models.test_chat_mapping import call, item

MARK = {"type": "ephemeral"}


def config(**changes) -> OpenAIChatConfig:
    return OpenAIChatConfig(model="m", **changes)


def growing_requests():
    base = model_request(with_tools=True).model_copy(
        update={"instructions": "约束" * 600, "consumed_usage": None, "affordable_requests": 3}
    )
    first = call()
    one = base.model_copy(
        update={
            "history": (
                *base.history,
                item(first),
                item(ToolResultContent(call_id=first.call_id, outcome="succeeded", output="a")),
            )
        }
    )
    second = call()
    two = one.model_copy(
        update={
            "history": (
                *one.history,
                item(second),
                item(ToolResultContent(call_id=second.call_id, outcome="succeeded", output="b")),
            ),
            "affordable_requests": 2,
        }
    )
    return one, two


def plain(message):
    content = message.get("content")
    if isinstance(content, list):
        (block,) = content
        return {**message, "content": block["text"]}
    return message


def unmarked(message):
    content = message.get("content")
    if isinstance(content, list):
        return {
            **message,
            "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in content],
        }
    return message


def test_default_layout_and_serialization_are_unchanged():
    one, _ = growing_requests()
    body, _ = build_request(one, config())
    assert body["messages"][0] == {"role": "system", "content": one.instructions}
    assert all(isinstance(m.get("content"), str | None) for m in body["messages"])
    assert "cache_control" not in str(body)
    assert "prompt_cache" not in config().model_dump(mode="json")


def test_explicit_tail_marks_the_stable_prefix_and_moves_volatile_notes_last():
    one, two = growing_requests()
    cached = config(prompt_cache="explicit_tail")
    default, _ = build_request(one, config())
    body, _ = build_request(one, cached)
    messages = body["messages"]
    assert messages[0]["content"][0]["cache_control"] == MARK
    assert messages[0]["content"][0]["text"] == one.instructions
    notes = [m for m in default["messages"][1:] if m["role"] == "system"]
    assert notes and messages[-len(notes) :] == notes
    marked = [i for i, m in enumerate(messages) if "cache_control" in str(m.get("content"))]
    assert len(marked) == 2 and marked[-1] < len(messages) - len(notes)
    # 只改变块形态与顺序，不改变任何文本。
    assert sorted(map(str, map(plain, messages))) == sorted(map(str, default["messages"]))
    # 下一请求的前缀与本请求逐条字节一致（只去掉移动的cache_control），使Provider可命中已建缓存。
    after, _ = build_request(two, cached)
    stable = len(messages) - len(notes)
    assert [unmarked(m) for m in after["messages"][:stable]] == [
        unmarked(m) for m in messages[:stable]
    ]


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
@pytest.mark.parametrize(("creation", "expected"), [(3, 3), (True, None), ("3", None)])
async def test_dashscope_cache_creation_field_is_parsed_only_as_an_integer(
    adapter, tmp_path: Path, creation, expected
):
    parts = adapter.wire.text_frames()
    value = adapter.wire.chunk(usage=True)
    value["usage"]["prompt_tokens_details"] = {
        "cached_tokens": 4,
        "cache_creation_input_tokens": creation,
    }
    parts[-2] = adapter.wire.frame(value)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    usage = turn.model_attempts[0].usage
    assert usage.cache_read_input_tokens == 4
    assert usage.cache_creation_input_tokens == expected
    assert usage.uncached_input_tokens == (10 - 4 - 3 if expected is not None else None)


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
@pytest.mark.parametrize(
    ("hit", "miss", "complete"),
    [(4, 6, True), (4, 5, False), (3, 7, False), (True, 6, False), (4, None, False)],
)
async def test_deepseek_hit_miss_split_prices_input_without_creation(
    adapter, tmp_path: Path, hit, miss, complete
):
    """DeepSeek自动缓存只报告命中/未命中；完整一致时创建为0，否则仍失败关闭为未知。"""
    parts = adapter.wire.text_frames()
    value = adapter.wire.chunk(usage=True)
    value["usage"]["prompt_tokens_details"] = {"cached_tokens": 4}
    value["usage"]["prompt_cache_hit_tokens"] = hit
    if miss is not None:
        value["usage"]["prompt_cache_miss_tokens"] = miss
    parts[-2] = adapter.wire.frame(value)
    turn, _, _ = await execute(adapter, tmp_path, parts)
    usage = turn.model_attempts[0].usage
    assert usage.cache_read_input_tokens == 4
    assert usage.cache_creation_input_tokens == (0 if complete else None)
    assert usage.uncached_input_tokens == (6 if complete else None)


@pytest.mark.parametrize("enabled", [False, True])
def test_thinking_type_format_sends_the_deepseek_switch(enabled):
    config = OpenAIChatConfig(model="m", enable_thinking=enabled, thinking_format="thinking_type")
    body, _ = build_request(model_request(), config)
    assert body["extra_body"] == {"thinking": {"type": "enabled" if enabled else "disabled"}}
    default, _ = build_request(model_request(), OpenAIChatConfig(model="m", enable_thinking=False))
    assert default["extra_body"] == {"enable_thinking": False}


def test_thinking_type_format_requires_an_explicit_switch():
    with pytest.raises(ValueError):
        OpenAIChatConfig(model="m", thinking_format="thinking_type")


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
@pytest.mark.parametrize("implicit", [False, True])
async def test_implicit_cache_usage_prices_unreported_creation_as_zero(
    adapter, tmp_path: Path, implicit
):
    """DashScope隐式缓存只报告命中数：声明隐式口径时创建计0，否则仍失败关闭为未知。"""
    parts = adapter.wire.text_frames()
    value = adapter.wire.chunk(usage=True)
    value["usage"]["prompt_tokens_details"] = {"cached_tokens": 4}
    parts[-2] = adapter.wire.frame(value)
    turn, _, _ = await execute(
        adapter, tmp_path, parts, prompt_cache="stable_tail", implicit_cache_usage=implicit
    )
    usage = turn.model_attempts[0].usage
    assert usage.cache_read_input_tokens == 4
    assert usage.cache_creation_input_tokens == (0 if implicit else None)
    assert usage.uncached_input_tokens == (6 if implicit else None)


@pytest.mark.parametrize("prompt_cache", [None, "explicit_tail"])
def test_implicit_cache_usage_requires_the_unmarked_automatic_layout(prompt_cache):
    with pytest.raises(ValueError):
        config(prompt_cache=prompt_cache, implicit_cache_usage=True)
    assert "implicit_cache_usage" not in config(prompt_cache="stable_tail").model_dump(mode="json")


@pytest.mark.parametrize("omit", [False, True])
def test_omitted_tool_choice_leaves_required_to_the_narrowed_catalog(omit):
    """端点强制选择不可靠时不发送tool_choice；DashScope思考开关与历史形态不变。"""
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={"tool_choice": "required", "forced_tool": request.tools[0].name}
    )
    thinking = config(
        enable_thinking=True,
        output_token_parameter="max_tokens",
        prompt_cache="stable_tail",
        omit_tool_choice=omit,
    )
    body, _ = build_request(request, thinking)
    assert body.get("tool_choice") == (None if omit else "required")
    assert body["extra_body"] == {"enable_thinking": True} and body["tools"]
    assert all("reasoning_content" not in message for message in body["messages"])
    assert "omit_tool_choice" not in config().model_dump(mode="json")


def _usage_on(frame_value, wire):
    value = dict(frame_value)
    value["usage"] = wire.chunk(usage=True)["usage"]
    return wire.frame(value)


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
async def test_usage_on_the_finishing_chunk_is_accepted(adapter, tmp_path: Path):
    """DeepSeek把用量放在携带finish_reason的末个分片中，而非独立的空choices分片。"""
    wire = adapter.wire
    parts = [
        wire.frame(wire.chunk({"role": "assistant", "content": "你"})),
        _usage_on(wire.chunk({"content": "好"}, finish="stop"), wire),
        b"data: [DONE]\n\n",
    ]
    turn, _, _ = await execute(adapter, tmp_path, parts)
    attempt = turn.model_attempts[0]
    assert attempt.status == "completed" and attempt.usage.completeness == "complete"


@pytest.mark.parametrize("adapter", ["openai"], indirect=True)
async def test_usage_on_an_unfinished_chunk_is_still_rejected(adapter, tmp_path: Path):
    wire = adapter.wire
    parts = [
        _usage_on(wire.chunk({"role": "assistant", "content": "你"}), wire),
        wire.frame(wire.chunk(finish="stop")),
        b"data: [DONE]\n\n",
    ]
    turn, _, _ = await execute(adapter, tmp_path, parts)
    attempt = turn.model_attempts[0]
    assert attempt.status == "failed"
    assert "usage_shape_or_order_invalid" in attempt.error.message


def test_stable_tail_keeps_the_history_prefix_and_moves_notes_to_the_end():
    """d4：说明插在指令与历史之间，DeepSeek自动缓存只命中指令前缀（10%～38%）。"""
    config = OpenAIChatConfig(model="m", prompt_cache="stable_tail")
    request = model_request(with_tools=True).model_copy(
        update={"instructions": "rules", "affordable_requests": 5}
    )
    first, _ = build_request(request, config)
    second, _ = build_request(request.model_copy(update={"affordable_requests": 4}), config)
    roles = [m["role"] for m in first["messages"]]
    assert first["messages"][0] == {"role": "system", "content": "rules"}
    assert roles[1] == "user"
    stable = len(first["messages"]) - sum(1 for m in first["messages"][1:] if m["role"] == "system")
    assert first["messages"][:stable] == second["messages"][:stable]
    assert all("cache_control" not in json.dumps(m) for m in first["messages"])


@pytest.mark.parametrize("thinking", [True, False])
def test_thinking_type_history_assistants_carry_reasoning_content(thinking):
    """v55t：思考模式带工具时历史助手消息缺reasoning_content，DeepSeek返回400。"""
    request = model_request(with_tools=True)
    tool_call = call()
    history = (
        *request.history,
        item(tool_call),
        item(ToolResultContent(call_id=tool_call.call_id, outcome="succeeded", output="x")),
    )
    config = OpenAIChatConfig(model="m", enable_thinking=thinking, thinking_format="thinking_type")
    body, _ = build_request(request.model_copy(update={"history": history}), config)
    assistants = [m for m in body["messages"] if m["role"] == "assistant"]
    assert assistants
    assert all(("reasoning_content" in m) is thinking for m in assistants)
    if thinking:
        assert all(m["reasoning_content"] == "" for m in assistants)


@pytest.mark.parametrize("thinking", [True, False])
def test_native_thinking_omits_unsupported_tool_choice(thinking):
    """v55t2：DeepSeek思考模式对required与指定函数的tool_choice返回400。"""
    base = model_request(with_tools=True)
    (tool,) = base.tools
    config = OpenAIChatConfig(model="m", enable_thinking=thinking, thinking_format="thinking_type")
    forced = base.model_copy(update={"tool_choice": "required", "forced_tool": tool.name})
    required = base.model_copy(update={"tool_choice": "required"})
    for request in (forced, required):
        body, _ = build_request(request, config)
        assert ("tool_choice" in body) is not thinking


def _tool_history_request():
    request = model_request(with_tools=True)
    tool_call = call()
    history = (
        *request.history,
        item(tool_call),
        item(ToolResultContent(call_id=tool_call.call_id, outcome="succeeded", output="x")),
    )
    return request.model_copy(update={"history": history, "instructions": "rules"})


def test_stable_tail_notes_are_trailing_user_messages_and_keep_the_prefix():
    """p1：DeepSeek把末尾system说明并入开头，Pro缓存命中仅13%～22%；改为末尾user消息后接近100%。"""
    config = OpenAIChatConfig(model="m", prompt_cache="stable_tail")
    request = _tool_history_request()
    first, _ = build_request(request.model_copy(update={"affordable_requests": 5}), config)
    second, _ = build_request(request.model_copy(update={"affordable_requests": 4}), config)
    roles = [m["role"] for m in first["messages"]]
    assert roles[0] == "system" and roles.count("system") == 1
    notes = [m for m in first["messages"] if str(m["content"]).startswith("[Harness note] ")]
    assert notes and all(m["role"] == "user" for m in notes)
    assert first["messages"][-len(notes) :] == notes
    stable = len(first["messages"]) - len(notes)
    assert first["messages"][:stable] == second["messages"][:stable]


@pytest.mark.parametrize(("layout", "expected"), [("stable_tail", "required"), (None, "function")])
def test_forced_choice_is_sent_as_required_under_the_stable_tail(layout, expected):
    """指定函数的tool_choice改写DeepSeek提示渲染（Pro命中归零），stable_tail下改发required。"""
    base = model_request(with_tools=True)
    (tool,) = base.tools
    forced = base.model_copy(update={"tool_choice": "required", "forced_tool": tool.name})
    body, _ = build_request(forced, OpenAIChatConfig(model="m", prompt_cache=layout))
    choice = body["tool_choice"]
    assert (choice == "required") if expected == "required" else (choice["type"] == "function")


@pytest.mark.parametrize("with_tools", [False, True])
def test_tool_step_thinking_disables_thinking_for_requests_without_tools(with_tools):
    """摘要与收尾回复不提供工具，其输出预算不足以容纳推理；只在工具步骤开启思考。"""
    request = model_request(with_tools=with_tools)
    scoped = config(
        enable_thinking=True, output_token_parameter="max_tokens", thinking_tool_steps_only=True
    )
    body, _ = build_request(request, scoped)
    assert body["extra_body"] == {"enable_thinking": with_tools}
    plain, _ = build_request(
        request, config(enable_thinking=True, output_token_parameter="max_tokens")
    )
    assert plain["extra_body"] == {"enable_thinking": True}


@pytest.mark.parametrize(
    "changes",
    [{"enable_thinking": False}, {"enable_thinking": True, "thinking_format": "thinking_type"}],
)
def test_tool_step_thinking_requires_an_explicit_dashscope_switch(changes):
    with pytest.raises(ValueError):
        config(thinking_tool_steps_only=True, **changes)
