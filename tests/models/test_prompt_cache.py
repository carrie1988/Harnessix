"""显式前缀缓存：稳定前缀在前并打标记、易变说明在末尾；默认映射与序列化不变。"""

from __future__ import annotations

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
