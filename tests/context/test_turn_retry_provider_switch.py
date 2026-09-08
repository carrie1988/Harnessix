from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import httpx
import httpx2
import pytest

from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import RETRY_INSTRUCTIONS, AgentRuntime
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.models import anthropic_wire
from tests.models import wire as openai_wire

ProviderKind = Literal["openai", "anthropic"]
KEY_ENV = "HARNESSIX_PROVIDER_SWITCH_TEST_KEY"
CANARY = "provider-switch-test-credential"


def _provider(
    kind: ProviderKind,
    handler: Any,
) -> OpenAIChatProvider | AnthropicProvider:
    if kind == "openai":
        return OpenAIChatProvider(
            OpenAIChatConfig(
                model="openai-model",
                api_key_env=KEY_ENV,
                max_attempts=1,
                retry_delay_seconds=0,
            ),
            transport=httpx.MockTransport(handler),
        )
    return AnthropicProvider(
        AnthropicConfig(
            model="anthropic-model",
            api_key_env=KEY_ENV,
            max_attempts=1,
            retry_delay_seconds=0,
        ),
        transport=httpx2.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("source_kind", "retry_kind"),
    [("openai", "anthropic"), ("anthropic", "openai")],
)
async def test_terminal_retry_switches_real_adapter_without_replaying_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: ProviderKind,
    retry_kind: ProviderKind,
) -> None:
    monkeypatch.setenv(KEY_ENV, CANARY)
    store = SQLiteSessionStore(tmp_path / f"{source_kind}-to-{retry_kind}.db")
    tools = RecordingTools()
    source_requests: list[dict[str, Any]] = []

    def source_handler(request: httpx.Request | httpx2.Request) -> httpx.Response | httpx2.Response:
        source_requests.append(json.loads(request.content))
        if len(source_requests) == 1:
            if source_kind == "openai":
                return openai_wire.response(openai_wire.WireStream(openai_wire.tool_frames()))
            return anthropic_wire.response(anthropic_wire.WireStream(anthropic_wire.tool_frames()))
        http = httpx if source_kind == "openai" else httpx2
        return http.Response(401, json={"error": {"type": "authentication_error"}})

    source_provider = _provider(source_kind, source_handler)
    async with source_provider:
        async with AgentRuntime(store, source_provider, tools) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            source = await runtime.run_turn(thread.thread_id, "读取后继续任务", request_id="source")

    assert source.status is TurnStatus.FAILED
    assert len(tools.calls) == 1
    source_call = next(
        item.content for item in source.items if isinstance(item.content, ToolCallContent)
    )
    native_call_id = "wire-call-0" if source_kind == "openai" else "toolu_0"
    assert source_call.provider_call_id == native_call_id

    retry_requests: list[dict[str, Any]] = []

    def retry_handler(request: httpx.Request | httpx2.Request) -> httpx.Response | httpx2.Response:
        retry_requests.append(json.loads(request.content))
        if retry_kind == "openai":
            return openai_wire.response(openai_wire.WireStream(openai_wire.text_frames()))
        return anthropic_wire.response(anthropic_wire.WireStream(anthropic_wire.text_frames()))

    retry_provider = _provider(retry_kind, retry_handler)
    async with retry_provider:
        async with AgentRuntime(store, retry_provider, tools) as runtime:
            retried = await runtime.retry_turn(
                thread.thread_id,
                source.turn_id,
                request_id="retry",
            )

    assert retried.status is TurnStatus.COMPLETED
    assert retried.retry_of_turn_id == source.turn_id
    assert len(tools.calls) == 1
    assert len(retry_requests) == 1
    encoded = json.dumps(retry_requests[0], ensure_ascii=False, sort_keys=True)
    stable_call_id = "call_" + source_call.call_id.hex
    assert stable_call_id in encoded
    assert native_call_id not in encoded
    assert RETRY_INSTRUCTIONS in encoded
    assert [attempt.provider for attempt in source.model_attempts] == [
        "openai_chat" if source_kind == "openai" else "anthropic",
        "openai_chat" if source_kind == "openai" else "anthropic",
    ]
    assert [attempt.provider for attempt in retried.model_attempts] == [
        "openai_chat" if retry_kind == "openai" else "anthropic"
    ]
    persisted = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == persisted
    assert CANARY not in "".join(
        event.model_dump_json() for event in await store.events(thread.thread_id)
    )
