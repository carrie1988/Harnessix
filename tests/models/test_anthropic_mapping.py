import json
from uuid import uuid4

import httpx2
import pytest

from harnessix.agent.models import Item, ItemStatus, TextContent, ToolResultContent
from harnessix.models._anthropic_mapping import build_request
from harnessix.models._history import InvalidModelRequest
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, ChatCapabilities
from tests.contracts.provider import model_request
from tests.models.test_chat_mapping import call, item


def test_parallel_results_form_one_user_message() -> None:
    request = model_request(with_tools=True)
    first, second = call(), call()
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                item(TextContent(kind="assistant_message", text="读取两个结果")),
                item(first),
                item(second),
                item(ToolResultContent(call_id=first.call_id, outcome="succeeded", output=1)),
                item(ToolResultContent(call_id=second.call_id, outcome="failed")),
            )
        }
    )
    body, names = build_request(request, AnthropicConfig(model="test"))
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    uses = messages[1]["content"][1:]
    results = messages[2]["content"]
    assert [b["id"] for b in uses] == [b["tool_use_id"] for b in results]
    assert len({b["id"] for b in uses}) == 2
    assert [b["is_error"] for b in results] == [False, True]
    assert json.loads(results[0]["content"])["output"] == 1
    assert names[body["tools"][0]["name"]] == "test.read"
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": False}


@pytest.mark.parametrize(
    "case",
    [
        "prefill",
        "empty",
        "unfinished",
        "duplicate_tool",
        "bad_schema",
        "size",
        "tool_capability",
        "history_capability",
    ],
)
def test_invalid_requests_rejected_locally(case: str) -> None:
    request = model_request(with_tools=True)
    config = AnthropicConfig(model="test", max_request_bytes=1024)
    if case == "prefill":
        request = request.model_copy(
            update={
                "history": (
                    *request.history,
                    item(TextContent(kind="assistant_message", text="prefill")),
                )
            }
        )
    elif case == "empty":
        request = request.model_copy(
            update={"history": (item(TextContent(kind="user_message", text="")),)}
        )
    elif case == "unfinished":
        request = request.model_copy(
            update={
                "history": (
                    Item(
                        item_id=uuid4(),
                        status=ItemStatus.STARTED,
                        content=TextContent(kind="user_message", text="任务"),
                    ),
                )
            }
        )
    elif case == "duplicate_tool":
        request = request.model_copy(update={"tools": request.tools * 2})
    elif case == "bad_schema":
        request = request.model_copy(
            update={
                "tools": (request.tools[0].model_copy(update={"input_schema": {"type": "array"}}),)
            }
        )
    elif case == "size":
        request = request.model_copy(
            update={"history": (item(TextContent(kind="user_message", text="x" * 2048)),)}
        )
    elif case in {"tool_capability", "history_capability"}:
        config = config.model_copy(
            update={"capabilities": ChatCapabilities(tool_calls=False, parallel_tool_calls=False)}
        )
        if case == "history_capability":
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
    with pytest.raises(InvalidModelRequest):
        build_request(request, config)


@pytest.mark.parametrize("case", ["missing", "invalid", "headers"])
def test_key_reference_fails_without_exposure(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    monkeypatch.delenv("HARNESSIX_TEST_MISSING_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    if case == "invalid":
        monkeypatch.setenv("HARNESSIX_TEST_MISSING_KEY", "fixture-CANARY\n")
    elif case == "headers":
        monkeypatch.setenv("HARNESSIX_TEST_MISSING_KEY", "fixture-CANARY")
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Secret: fixture-CANARY")
    with pytest.raises(ValueError) as failure:
        AnthropicProvider(
            AnthropicConfig(model="test", api_key_env="HARNESSIX_TEST_MISSING_KEY"),
            transport=httpx2.MockTransport(lambda _: pytest.fail("不可发网")),
        )
    assert "fixture-CANARY" not in str(failure.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:key@example.com",
        "https://example.com/?key=a",
        "https://example.com/#x",
        "https://example.com:bad",
    ],
)
def test_https_endpoint_constraints(url: str) -> None:
    with pytest.raises(ValueError):
        AnthropicConfig(model="test", base_url=url)
