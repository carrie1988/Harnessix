from uuid import uuid4

import pytest

from harnessix.agent.models import (
    Item,
    ItemContent,
    ItemStatus,
    PlanContent,
    PlanStep,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.domain.models import EffectClass
from harnessix.models._chat_mapping import (
    InvalidModelRequest,
    build_request,
    messages_for,
    tool_alias,
)
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from tests.contracts.provider import model_request


def item(content: ItemContent) -> Item:
    return Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)


def call() -> ToolCallContent:
    return ToolCallContent(
        call_id=uuid4(),
        provider_call_id="repeated",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
    )


def test_parallel_history_and_stable_aliases() -> None:
    first, second = call(), call()
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                item(TextContent(kind="assistant_message", text="读取")),
                item(first),
                item(second),
                item(ToolResultContent(call_id=first.call_id, outcome="succeeded", output=1)),
                item(ToolResultContent(call_id=second.call_id, outcome="succeeded", output=2)),
                item(TextContent(kind="assistant_message", text="完成")),
            )
        }
    )
    body, names = build_request(
        request, OpenAIChatConfig(model="test", output_token_parameter="max_tokens")
    )
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "assistant"]
    assert messages[1]["content"] == "读取"
    assert len(messages[1]["tool_calls"]) == 2
    assert messages[1]["tool_calls"][0]["id"] != messages[1]["tool_calls"][1]["id"]
    assert names[body["tools"][0]["function"]["name"]] == "test.read"
    assert "max_tokens" in body and "max_completion_tokens" not in body
    assert tool_alias("test.read") != tool_alias("test_read")
    assert len(tool_alias("工具名" * 50)) <= 64


@pytest.mark.parametrize(
    "case",
    [
        "orphan",
        "duplicate",
        "missing",
        "interrupted",
        "reused",
        "unfinished",
        "summary",
        "plan",
        "empty",
    ],
)
def test_invalid_history_fails_explicitly(case: str) -> None:
    first, second = call(), call()
    result = ToolResultContent(call_id=first.call_id, outcome="succeeded")
    histories = {
        "orphan": (item(result),),
        "duplicate": (item(first), item(result), item(result)),
        "missing": (item(first),),
        "interrupted": (
            item(first),
            item(second),
            item(result),
            item(TextContent(kind="user_message", text="新任务")),
        ),
        "reused": (item(first), item(result), item(first), item(result)),
        "unfinished": (
            item(TextContent(kind="user_message", text="任务")).model_copy(
                update={"status": ItemStatus.STARTED}
            ),
        ),
        "summary": (item(TextContent(kind="reasoning_summary", text="摘要")),),
        "plan": (item(PlanContent(steps=(PlanStep(step_id="1", description="设计"),))),),
        "empty": (),
    }
    with pytest.raises(InvalidModelRequest):
        messages_for(model_request().model_copy(update={"history": histories[case]}))


@pytest.mark.parametrize(
    "case", ["duplicate_tool", "bad_schema", "request_size", "history_capability"]
)
def test_invalid_request_is_rejected_before_network(case: str) -> None:
    request = model_request(with_tools=True)
    config = OpenAIChatConfig(model="test", max_request_bytes=1024)
    if case == "duplicate_tool":
        request = request.model_copy(update={"tools": request.tools * 2})
    elif case == "bad_schema":
        request = request.model_copy(
            update={
                "tools": (request.tools[0].model_copy(update={"input_schema": {"type": "array"}}),)
            }
        )
    elif case == "request_size":
        request = request.model_copy(
            update={"history": (item(TextContent(kind="user_message", text="x" * 2000)),)}
        )
    elif case == "history_capability":
        first = call()
        request = request.model_copy(
            update={
                "tools": (),
                "history": (
                    *request.history,
                    item(first),
                    item(ToolResultContent(call_id=first.call_id, outcome="succeeded")),
                ),
            }
        )
        config = config.model_copy(
            update={"capabilities": ChatCapabilities(tool_calls=False, parallel_tool_calls=False)}
        )
    with pytest.raises(InvalidModelRequest):
        build_request(request, config)
