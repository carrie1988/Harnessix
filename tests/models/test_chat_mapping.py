import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import AgentFailure, FailureCategory
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
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import InvalidModelRequest, messages_for, tool_alias
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.contracts import ModelRequest
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
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"].startswith("Current request tool catalog")
    messages = body["messages"][1:]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "assistant"]
    assert messages[1]["content"] == "读取"
    assert len(messages[1]["tool_calls"]) == 2
    assert messages[1]["tool_calls"][0]["id"] != messages[1]["tool_calls"][1]["id"]
    assert names[body["tools"][0]["function"]["name"]] == "test.read"
    assert "max_tokens" in body and "max_completion_tokens" not in body
    assert tool_alias("test.read") != tool_alias("test_read")
    assert len(tool_alias("工具名" * 50)) <= 64


def test_failed_tool_result_keeps_actionable_error_on_openai_wire() -> None:
    invoked = call()
    failure = AgentFailure(
        code="tool_expected_revision_required",
        message="read_file后续页必须携带上一成功结果的revision作为expected_revision",
    )
    request = model_request(with_tools=True)
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                item(invoked),
                item(ToolResultContent(call_id=invoked.call_id, outcome="failed", error=failure)),
            )
        }
    )
    body, _ = build_request(
        request, OpenAIChatConfig(model="test", output_token_parameter="max_tokens")
    )
    result = body["messages"][-1]
    content = json.loads(result["content"])
    assert result["role"] == "tool"
    assert content["error"] == {
        "code": failure.code,
        "message": failure.message,
        "retryable": False,
        "category": FailureCategory.TOOL.value,
    }


def test_instructions_are_a_separate_leading_system_message() -> None:
    request = model_request().model_copy(update={"instructions": "runtime > user > project"})
    body, _ = build_request(
        request, OpenAIChatConfig(model="test", output_token_parameter="max_tokens")
    )
    assert body["messages"][0] == {
        "role": "system",
        "content": "runtime > user > project",
    }
    assert body["messages"][1]["role"] == "user"


@pytest.mark.parametrize("forced", [False, True])
def test_required_tool_choice_is_projected_to_openai_wire(forced: bool) -> None:
    request = model_request(with_tools=True)
    tool_name = request.tools[0].name
    request = request.model_copy(
        update={
            "tool_choice": "required",
            "forced_tool": tool_name if forced else None,
        }
    )

    body, _ = build_request(request, OpenAIChatConfig(model="test"))

    assert body["tool_choice"] == (
        {"type": "function", "function": {"name": tool_alias(tool_name)}} if forced else "required"
    )


@pytest.mark.parametrize("case", ["required_without_tools", "forced_outside_catalog"])
def test_model_request_rejects_invalid_tool_choice(case: str) -> None:
    request = model_request(with_tools=case == "forced_outside_catalog")
    body = request.model_dump(mode="python")
    body["tool_choice"] = "required"
    if case == "forced_outside_catalog":
        body["forced_tool"] = "missing"

    with pytest.raises(ValidationError, match="工具"):
        ModelRequest.model_validate(body, strict=True)


@pytest.mark.parametrize("enabled", [True, False])
def test_explicit_thinking_mode_is_forwarded_as_provider_extra_body(enabled: bool) -> None:
    body, _ = build_request(
        model_request(), OpenAIChatConfig(model="test", enable_thinking=enabled)
    )

    assert body["extra_body"] == {"enable_thinking": enabled}


def test_unspecified_thinking_mode_keeps_provider_default() -> None:
    body, _ = build_request(model_request(), OpenAIChatConfig(model="test"))

    assert "extra_body" not in body


def test_unspecified_thinking_mode_preserves_legacy_serialized_config() -> None:
    legacy = OpenAIChatConfig(model="test").model_dump(mode="json")
    explicit = OpenAIChatConfig(model="test", enable_thinking=False).model_dump(mode="json")

    assert "enable_thinking" not in legacy
    assert explicit["enable_thinking"] is False
    assert "thinking_budget" not in legacy
    assert "thinking_budget" not in explicit


def test_bounded_thinking_uses_total_completion_limit() -> None:
    config = OpenAIChatConfig(
        model="test", enable_thinking=True, thinking_budget=1024, max_output_tokens=4096
    )
    body, _ = build_request(model_request(), config)

    assert body["extra_body"] == {"enable_thinking": True, "thinking_budget": 1024}
    assert body["max_completion_tokens"] == 4096
    assert "max_tokens" not in body


@pytest.mark.parametrize("remaining", [2, 1024, 1025, 4096])
def test_thinking_ceiling_shrinks_with_remaining_total_output(remaining: int) -> None:
    request = model_request().model_copy(update={"remaining_tokens": remaining})
    config = OpenAIChatConfig(
        model="test", enable_thinking=True, thinking_budget=1024, max_output_tokens=4096
    )

    body, _ = build_request(request, config)

    assert body["max_completion_tokens"] == remaining
    assert body["extra_body"]["thinking_budget"] == min(1024, remaining - 1)


def test_thinking_without_room_for_an_answer_is_rejected_before_request() -> None:
    request = model_request().model_copy(update={"remaining_tokens": 1})
    config = OpenAIChatConfig(model="test", enable_thinking=True, thinking_budget=512)

    with pytest.raises(InvalidModelRequest):
        build_request(request, config)


@pytest.mark.parametrize(
    "updates",
    [
        {"thinking_budget": 0},
        {"thinking_budget": True},
        {"thinking_budget": 262145},
        {"thinking_budget": 4096},
        {"enable_thinking": False},
        {"enable_thinking": None},
        {"output_token_parameter": "max_tokens"},
    ],
)
def test_explicit_thinking_budget_rejects_unbounded_or_answerless_configuration(updates) -> None:
    with pytest.raises(ValidationError):
        OpenAIChatConfig.model_validate(
            {
                "model": "test",
                "enable_thinking": True,
                "thinking_budget": 1024,
                "max_output_tokens": 4096,
                **updates,
            },
            strict=True,
        )


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
