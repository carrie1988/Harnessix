"""真实OpenAI SDK离线线协议进入SQLite计划，再回到配对模型历史。"""

import json

import httpx

from harnessix.agent.models import Budget, PlanContent, TurnStatus, Usage
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames


async def test_sdk_tool_group_plan_sqlite_and_history_keep_original_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANNING_OFFLINE_TEST_KEY", "offline-only-placeholder")
    requests = []
    streams = []
    plan_call = call(1, '{"steps":[{"step_id":"inspect","description":"检查证据"}]}')
    plan_call["function"]["name"] = "update_plan"
    parts = [
        frame(chunk({"tool_calls": [call(), plan_call]})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        wire = WireStream(parts if len(requests) == 1 else text_frames())
        streams.append(wire)
        return response(wire)

    store = SQLiteSessionStore(tmp_path / "session.db")
    tools = RecordingTools(parallel=True)
    config = OpenAIChatConfig(
        model="test-model",
        api_key_env="PLANNING_OFFLINE_TEST_KEY",
        max_attempts=1,
        capabilities=ChatCapabilities(tool_calls=True, parallel_tool_calls=True),
    )
    async with OpenAIChatProvider(config, transport=httpx.MockTransport(handler)) as provider:
        async with AgentRuntime(store, provider, tools, enable_planning=True) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id,
                "离线线协议验证",
                request_id="sdk",
                budget=Budget(max_tokens=50_000, max_steps=16, timeout_seconds=900),
            )

    assert turn.status is TurnStatus.COMPLETED
    assert turn.usage == Usage(input_tokens=20, output_tokens=4)
    assert turn.model_steps == len(requests) == 2
    assert len(tools.calls) == 1 and tools.calls[0].tool == "test.read"
    assert sum(isinstance(item.content, PlanContent) for item in turn.items) == 1
    assert all(stream.closed for stream in streams)
    assert turn.budget.max_tokens == 50_000 and turn.budget.max_steps == 16
    history = requests[1]["messages"]
    assistant = next(message for message in history if message.get("tool_calls"))
    assert [call["function"]["name"] for call in assistant["tool_calls"]][-1] == "update_plan"
    assert "检查证据" in assistant["tool_calls"][-1]["function"]["arguments"]
    results = [message for message in history if message["role"] == "tool"]
    assert len(results) == 2
    assert json.loads(results[-1]["content"])["output"]["self_reported"] is True
    assert not any(message.get("content_type") == "plan" for message in history)
    assert "offline-only-placeholder" not in json.dumps(history)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
