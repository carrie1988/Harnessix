import json

import httpx
import httpx2
import pytest

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.models import anthropic_wire as aw
from tests.models import wire as ow
from tests.patches.test_agent_bridge import case as case
from tests.patches.test_kernel_patch import approval_of, decide, results


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
async def test_sdk_read_patch_approval_reopen_readback_and_wire_privacy(
    case, tmp_path, monkeypatch, vendor
):
    source, factory, copy = case
    monkeypatch.setenv("HARNESSIX_PATCH_FIXTURE_KEY", "offline-fixture-not-a-credential")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    requests, streams = [], []

    def tool_outputs(body):
        if vendor == "openai":
            return [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        return [
            json.loads(c["content"])
            for m in body["messages"]
            if isinstance(m["content"], list)
            for c in m["content"]
            if c["type"] == "tool_result"
        ]

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        output = tool_outputs(body)
        if index in (1, 3):
            name, args = "read_file", {"path": "main.py"}
            if index == 3:
                assert (
                    output[-1]["outcome"] == "succeeded"
                    and output[-1]["output"]["state"] == "applied"
                )
        elif index == 2:
            assert output[-1]["output"]["text"] == "before\r\n"
            name, args = (
                "apply_patch",
                {
                    "path": "main.py",
                    "expected_revision": output[-1]["output"]["revision"],
                    "edits": [{"old_text": "before", "new_text": "after"}],
                },
            )
        else:
            assert index == 4 and output[-1]["output"]["text"] == "after\r\n"
            name, args = None, None
        wire = ow if vendor == "openai" else aw
        if name is None:
            parts = wire.text_frames()
        elif vendor == "openai":
            parts = [
                ow.frame(
                    ow.chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call-{index}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_alias(name),
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ]
                        }
                    )
                ),
                ow.frame(ow.chunk(finish="tool_calls")),
                ow.frame(ow.chunk(usage=True)),
                b"data: [DONE]\n\n",
            ]
        else:
            parts = [
                aw.start(),
                aw.frame(
                    "content_block_start",
                    index=0,
                    content_block={
                        "type": "tool_use",
                        "id": f"toolu_{index}",
                        "name": tool_alias(name),
                        "input": {},
                    },
                ),
                aw.frame(
                    "content_block_delta",
                    index=0,
                    delta={"type": "input_json_delta", "partial_json": json.dumps(args)},
                ),
                aw.frame("content_block_stop", index=0),
                *aw.stop("tool_use"),
            ]
        stream = wire.WireStream(parts)
        streams.append(stream)
        return wire.response(stream)

    def provider():
        if vendor == "openai":
            return OpenAIChatProvider(
                OpenAIChatConfig(
                    model="test-model",
                    base_url="https://provider.invalid/v1",
                    api_key_env="HARNESSIX_PATCH_FIXTURE_KEY",
                    max_attempts=1,
                ),
                transport=httpx.MockTransport(handle),
            )
        return AnthropicProvider(
            AnthropicConfig(
                model="test-model",
                base_url="https://provider.invalid",
                api_key_env="HARNESSIX_PATCH_FIXTURE_KEY",
                max_attempts=1,
            ),
            transport=httpx2.MockTransport(handle),
        )

    store = SQLiteSessionStore(tmp_path / "s.db")
    async with (
        ManagedPatchBridge(copy) as bridge,
        CodingToolRuntime(copy.workspace.root) as reads,
        provider() as model,
    ):
        async with AgentRuntime(store, model, scoped_tools=reads, patches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(
                thread.thread_id, "把 before 改成 after，读回确认", request_id="sdk-patch"
            )
            assert waiting.status == TurnStatus.WAITING_APPROVAL and len(requests) == 2
            content = approval_of(waiting)
            assert copy.get(content.plan.plan_id).state == "pending"
    copy.close()
    with factory.open(content.plan.workspace_id) as reopened:
        async with (
            ManagedPatchBridge(reopened) as bridge,
            CodingToolRuntime(reopened.workspace.root) as reads,
            provider() as model,
        ):
            async with AgentRuntime(store, model, scoped_tools=reads, patches=bridge) as runtime:
                assert len(requests) == 2
                await decide(runtime, thread.thread_id, waiting)
                assert reopened.get(content.plan.plan_id).state == "pending"
                turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                assert turn.status == TurnStatus.COMPLETED
    assert len(requests) == 4 and all(stream.closed for stream in streams)
    assert len(results(turn)) == 3 and results(turn)[1].patch.state == "applied"
    public = json.dumps(requests)
    for private in (
        str(content.plan.plan_id),
        str(content.plan.workspace_id),
        content.plan.approval_fingerprint,
        content.plan.backend_fingerprint,
        content.plan.request_id,
        "kernel-managed-patch/v1",
        '"patch":',
    ):
        assert private not in public
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
    assert (source.root / "main.py").read_bytes() == b"before\r\n"
