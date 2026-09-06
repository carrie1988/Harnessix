import json
from contextlib import AsyncExitStack

import httpx
import httpx2
import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.models import anthropic_wire
from tests.models import wire as openai_wire


def _result_payloads(provider_name, body):
    if provider_name == "openai":
        return [
            json.loads(message["content"])
            for message in body["messages"]
            if message["role"] == "tool"
        ]
    return [
        json.loads(block["content"])
        for message in body["messages"]
        for block in message["content"]
        if block["type"] == "tool_result"
    ]


def _openai_tool_frames(step, arguments):
    response_id = f"paging-{step}"
    return [
        openai_wire.frame(
            openai_wire.chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"paging-call-{step}",
                            "type": "function",
                            "function": {
                                "name": tool_alias("read_file"),
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                },
                response_id=response_id,
            )
        ),
        openai_wire.frame(openai_wire.chunk(finish="tool_calls", response_id=response_id)),
        openai_wire.frame(openai_wire.chunk(usage=True, response_id=response_id)),
        b"data: [DONE]\n\n",
    ]


def _anthropic_tool_frames(step, arguments):
    return [
        anthropic_wire.start(),
        anthropic_wire.frame(
            "content_block_start",
            index=0,
            content_block={
                "type": "tool_use",
                "id": f"paging-call-{step}",
                "name": tool_alias("read_file"),
                "input": {},
            },
        ),
        anthropic_wire.frame(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": json.dumps(arguments)},
        ),
        anthropic_wire.frame("content_block_stop", index=0),
        *anthropic_wire.stop("tool_use"),
    ]


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
async def test_sdk_kernel_corrects_missing_paging_revision_and_replays(
    tmp_path, monkeypatch, provider_name
):
    monkeypatch.setenv("HARNESSIX_PAGING_FIXTURE_KEY", "fixture-key")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("第一行\n第二行\n", encoding="utf-8")
    requests = []
    wires = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        results = _result_payloads(provider_name, body)
        step = len(requests)
        if step == 1:
            arguments = {"path": "main.py", "max_lines": 1}
        elif step == 2:
            assert results[-1]["outcome"] == "succeeded"
            arguments = {"path": "main.py", "start_line": 2}
        elif step == 3:
            assert results[-1]["error"]["code"] == "tool_expected_revision_required"
            assert results[-1]["error"]["category"] == "tool"
            arguments = {
                "path": "main.py",
                "start_line": 2,
                "expected_revision": results[0]["output"]["revision"],
            }
        else:
            assert step == 4
            assert results[-1]["output"]["text"] == "第二行\n"
            parts = (
                openai_wire.text_frames()
                if provider_name == "openai"
                else anthropic_wire.text_frames()
            )
            wire = (
                openai_wire.WireStream(parts)
                if provider_name == "openai"
                else anthropic_wire.WireStream(parts)
            )
            wires.append(wire)
            return (
                openai_wire.response(wire)
                if provider_name == "openai"
                else anthropic_wire.response(wire)
            )
        parts = (
            _openai_tool_frames(step, arguments)
            if provider_name == "openai"
            else _anthropic_tool_frames(step, arguments)
        )
        wire = (
            openai_wire.WireStream(parts)
            if provider_name == "openai"
            else anthropic_wire.WireStream(parts)
        )
        wires.append(wire)
        return (
            openai_wire.response(wire)
            if provider_name == "openai"
            else anthropic_wire.response(wire)
        )

    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AsyncExitStack() as stack:
        if provider_name == "openai":
            provider = await stack.enter_async_context(
                OpenAIChatProvider(
                    OpenAIChatConfig(
                        model="test-model",
                        api_key_env="HARNESSIX_PAGING_FIXTURE_KEY",
                        max_attempts=1,
                    ),
                    transport=httpx.MockTransport(handle),
                )
            )
        else:
            provider = await stack.enter_async_context(
                AnthropicProvider(
                    AnthropicConfig(
                        model="test-model",
                        api_key_env="HARNESSIX_PAGING_FIXTURE_KEY",
                        max_attempts=1,
                    ),
                    transport=httpx2.MockTransport(handle),
                )
            )
        tools = await stack.enter_async_context(CodingToolRuntime(root))
        runtime = await stack.enter_async_context(AgentRuntime(store, provider, tools))
        thread = await runtime.create_thread(str(root))
        turn = await runtime.run_turn(thread.thread_id, "分页读取", request_id=provider_name)

    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert turn.status == TurnStatus.COMPLETED
    assert [result.outcome for result in results] == ["succeeded", "failed", "succeeded"]
    assert len(requests) == 4 and all(wire.closed for wire in wires)
    reopened = SQLiteSessionStore(store.path)
    await reopened.initialize()
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )
