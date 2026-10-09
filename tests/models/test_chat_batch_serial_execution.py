"""合成wire验证多调用接收与执行并发分离；不是百炼兼容性或Beta验收。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from harnessix.agent.models import Budget, ToolResultContent
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.config import ChatCapabilities
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools
from tests.agent.test_tool_scheduling import ParallelReads
from tests.models.test_openai_chat import config
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames


def batch_frames(*, invalid_last=False, missing_done=False):
    calls = [call(0, '{"value":1}'), call(1, '{"value":2}')]
    if invalid_last:
        calls[1]["function"]["arguments"] = '{"value":'
    frames = [
        frame(chunk({"tool_calls": calls})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
    ]
    return frames if missing_done else [*frames, b"data: [DONE]\n\n"]


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_batch_capability_does_not_choose_execution_concurrency(tmp_path, concurrency):
    """同一双调用响应：并发上限1时顺序执行，上限2时才允许重叠。"""
    tools = ParallelReads(expected=concurrency)
    wires = [WireStream(batch_frames()), WireStream(text_frames())]
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return response(wires[len(requests) - 1])

    store = SQLiteSessionStore(tmp_path / "session.sqlite")
    async with OpenAIChatProvider(
        config(max_attempts=1, capabilities=ChatCapabilities(parallel_tool_calls=True)),
        transport=httpx.MockTransport(handle),
    ) as provider:
        async with AgentRuntime(store, provider, tools, max_parallel_tools=concurrency) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            running = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "读取两项", request_id="batch-read")
            )
            try:
                await asyncio.wait_for(tools.entered.wait(), 10)
                assert len(tools.calls) == concurrency
            finally:
                tools.release.set()
            turn = await running
    assert turn.status == "completed" and tools.peak == concurrency and tools.active == 0
    assert [c.arguments["value"] for c in tools.calls] == [1, 2]
    results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert [r.output for r in results] == [{"value": 1}, {"value": 2}]
    assert len(requests) == 2 and all(r["parallel_tool_calls"] is True for r in requests)
    expected_ids = ["call_" + call.call_id.hex for call in tools.calls]
    assert [
        m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"
    ] == expected_ids
    assert [
        c["id"] for m in requests[1]["messages"] for c in m.get("tool_calls", [])
    ] == expected_ids
    assert all(wire.closed for wire in wires)
    reopened = SQLiteSessionStore(store.path)
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("failure", ["disabled", "invalid_last", "missing_done", "call_limit"])
async def test_batch_configuration_preserves_atomic_validation(tmp_path, failure):
    wire = WireStream(
        batch_frames(invalid_last=failure == "invalid_last", missing_done=failure == "missing_done")
    )
    sent = 0

    def handle(request):
        nonlocal sent
        sent += 1
        assert json.loads(request.content)["parallel_tool_calls"] is (failure != "disabled")
        return response(wire)

    tools = RecordingTools(parallel=True)
    async with OpenAIChatProvider(
        config(
            max_attempts=3, capabilities=ChatCapabilities(parallel_tool_calls=failure != "disabled")
        ),
        transport=httpx.MockTransport(handle),
    ) as provider:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.sqlite"), provider, tools, max_parallel_tools=1
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(
                thread.thread_id,
                "拒绝不完整整组",
                request_id="reject-batch",
                budget=Budget(max_tool_calls_per_step=1 if failure == "call_limit" else 8),
            )
    assert turn.status == "failed" and tools.calls == [] and sent == 1 and wire.closed
    assert len(turn.model_attempts) == 1
    assert turn.model_attempts[0].error.code == "provider_invalid_provider_output"
    if failure == "disabled":
        assert turn.model_attempts[0].error.message.endswith(
            "chat_protocol/v1:parallel_tool_calls_disabled"
        )


async def test_serial_batch_cancellation_never_starts_second_call(tmp_path):
    tools = ParallelReads(expected=1)
    wire = WireStream(batch_frames())
    sent = 0

    def handle(request):
        nonlocal sent
        sent += 1
        return response(wire)

    async with OpenAIChatProvider(
        config(max_attempts=1), transport=httpx.MockTransport(handle)
    ) as provider:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.sqlite"), provider, tools, max_parallel_tools=1
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            running = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "取消整组", request_id="cancel-batch")
            )
            try:
                await asyncio.wait_for(tools.entered.wait(), 10)
                snapshot = await runtime.store.get_thread(thread.thread_id)
                await runtime.cancel(thread.thread_id, snapshot.active_turn_id)
                turn = await asyncio.wait_for(running, 10)
            finally:
                tools.release.set()
    assert turn.status == "cancelled" and len(tools.calls) == 1 and tools.peak == 1
    assert tools.active == 0 and sent == 1 and wire.closed
