"""改写视图的输入上界：只免计同位置未变消息，缩短内容不抵扣，零 I/O。"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

from harnessix.agent.model_input_budget import INCREMENTAL_REQUEST_OVERHEAD_TOKENS
from harnessix.agent.models import TextContent, ToolResultContent
from harnessix.models._chat_input_anchor import CompletedChatInput
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import InvalidModelRequest, encode_json
from harnessix.models.contracts import RewrittenInputTokenEstimator
from tests.contracts.provider import model_request
from tests.models.test_chat_mapping import call, item
from tests.models.test_provider_input_budget import pure_provider


def wire_bytes(value) -> int:
    return len(encode_json(value).encode("utf-8"))


def result(source, output) -> object:
    return item(ToolResultContent(call_id=source.call_id, outcome="succeeded", output=output))


def masked_fixture():
    """上一请求含两次大读取；本次把较旧读取换成占位并追加一条新消息。"""
    provider = pure_provider()
    first, second = call(), call()
    base = model_request(with_tools=True).model_copy(update={"instructions": "约束" * 1000})
    old_result = result(first, {"text": "旧源码" * 2000})
    previous = base.model_copy(
        update={
            "history": (
                *base.history,
                item(first),
                old_result,
                item(second),
                result(second, {"text": "新源码" * 500}),
            )
        }
    )
    stub = old_result.model_copy(
        update={
            "content": old_result.content.model_copy(
                update={"output": {"omitted": "read_file lines 1-60", "revision": 3}}
            )
        }
    )
    current = previous.model_copy(
        update={
            "step": previous.step + 1,
            "history": (
                *previous.history[:2],
                stub,
                *previous.history[3:],
                item(TextContent(kind="assistant_message", text="继续")),
            ),
        }
    )
    body, _ = build_request(previous, provider.config)
    provider._completed_input = CompletedChatInput.capture(previous, body, provider.config)
    return provider, previous, current


def test_provider_declares_rewritten_capability():
    assert isinstance(pure_provider(), RewrittenInputTokenEstimator)


def test_masked_view_charges_only_stub_new_message_and_unproven_notes():
    provider, previous, current = masked_fixture()
    bound = provider.estimate_rewritten_input_tokens(current, previous.history)
    current_body, _ = build_request(current, provider.config)
    history_only = provider._history_body(current.history, current)["messages"]
    stub_message, appended_message = history_only[2], history_only[-1]
    # 只读未变消息与已证明的工具/指令免计；占位、追加消息与动态说明全额计入。
    assert bound < wire_bytes(current_body) // 4
    assert bound >= wire_bytes(stub_message) + wire_bytes(appended_message)
    assert bound >= INCREMENTAL_REQUEST_OVERHEAD_TOKENS


def test_shortened_message_is_charged_not_credited():
    provider, previous, current = masked_fixture()
    unmasked = current.model_copy(update={"history": (*previous.history, current.history[-1])})
    masked_bound = provider.estimate_rewritten_input_tokens(current, previous.history)
    unchanged_bound = provider.estimate_rewritten_input_tokens(unmasked, previous.history)
    # 遮蔽后的上界不得低于“旧内容全部保留”时的上界：删减内容不抵扣已计入用量。
    assert masked_bound > unchanged_bound


def test_append_only_view_is_never_cheaper_than_proven_prefix_path():
    provider, previous, current = masked_fixture()
    appended = current.model_copy(update={"history": (*previous.history, current.history[-1])})
    prefix = provider.estimate_input_tokens(appended, prefix_items=len(previous.history))
    rewritten = provider.estimate_rewritten_input_tokens(appended, previous.history)
    assert prefix <= rewritten <= prefix + 256


def test_changed_earlier_message_is_charged_in_full():
    provider, previous, current = masked_fixture()
    changed_user = item(TextContent(kind="user_message", text="改写任务" * 500))
    changed = current.model_copy(update={"history": (changed_user, *current.history[1:])})
    baseline = provider.estimate_rewritten_input_tokens(current, previous.history)
    bound = provider.estimate_rewritten_input_tokens(changed, previous.history)
    user_message = provider._history_body(changed.history, changed)["messages"][0]
    assert bound >= baseline + wire_bytes(user_message)


def test_shifted_positions_never_reuse_earlier_credit():
    provider, previous, current = masked_fixture()
    # 前插一条消息使后续位置全部错位：同位置不再相等，因此历史全部按字节计入。
    shifted = current.model_copy(
        update={"history": (item(TextContent(kind="user_message", text="前插")), *current.history)}
    )
    bound = provider.estimate_rewritten_input_tokens(shifted, previous.history)
    history = provider._history_body(shifted.history, shifted)["messages"]
    assert bound >= sum(wire_bytes(message) for message in history)


@pytest.mark.parametrize("drift", ["cold", "tools", "instructions", "thread"])
def test_unproven_wrappers_are_charged_in_full(drift):
    from uuid import uuid4

    provider, previous, current = masked_fixture()
    warm = provider.estimate_rewritten_input_tokens(current, previous.history)
    if drift == "cold":
        provider._completed_input = None
    elif drift == "tools":
        current = current.model_copy(
            update={"tools": (current.tools[0].model_copy(update={"description": "新" * 500}),)}
        )
    elif drift == "instructions":
        current = current.model_copy(update={"instructions": "新约束" * 1000})
    else:
        current = current.model_copy(update={"thread_id": uuid4()})
    body, _ = build_request(current, provider.config)
    guarded = provider.estimate_rewritten_input_tokens(current, previous.history)
    if drift == "tools":
        assert guarded >= wire_bytes(body["tools"])
    elif drift == "instructions":
        assert guarded >= wire_bytes({"role": "system", "content": current.instructions})
    else:
        assert guarded >= warm + wire_bytes(body["tools"])


def test_empty_previous_view_is_rejected():
    provider, _, current = masked_fixture()
    with pytest.raises(InvalidModelRequest):
        provider.estimate_rewritten_input_tokens(current, ())


def test_estimate_is_pure_without_network_or_credentials():
    provider, previous, current = masked_fixture()
    blocked = Mock(side_effect=AssertionError("forbidden I/O"))
    with ExitStack() as stack:
        for target in (
            "socket.socket",
            "socket.create_connection",
            "harnessix.models.openai_chat.read_key",
            "harnessix.models.openai_chat.validate_key",
            "harnessix.models.openai_chat.AsyncOpenAI",
        ):
            stack.enter_context(patch(target, blocked))
        provider.estimate_rewritten_input_tokens(current, previous.history)
    blocked.assert_not_called()
