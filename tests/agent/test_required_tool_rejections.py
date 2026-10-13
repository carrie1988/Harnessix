"""required/forced目录拒绝的有界纠正与失败关闭；只使用离线Provider。"""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from harnessix.agent.models import (
    Budget,
    ItemStatus,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStatus,
    Usage,
)
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_rejections import rejection_result, require_closed_rejections
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_tool_rejection_runtime import rejected_step


@pytest.fixture(params=[None, "test.read"], ids=["required", "forced"])
def forced_tool(request):
    return request.param


class RequiredUntilAccepted:
    def __init__(self, forced_tool):
        self.forced_tool = forced_tool

    def select(self, turn, definitions):
        visible = definitions
        if any(isinstance(item.content, ToolCallContent) for item in turn.items):
            return ModelToolSelection(visible)
        return ModelToolSelection(visible, "required", self.forced_tool)


async def run_required(tmp_path, provider, forced_tool, *, budget=None, tools=None):
    tools = tools or RecordingTools()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, provider, tools, model_tool_view=RequiredUntilAccepted(forced_tool)
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "有界工具纠正", request_id="r", budget=budget
        )
    restored = await SQLiteSessionStore(store.path).get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == restored
    require_closed_rejections(restored)
    assert provider.closed_streams == len(provider.requests)
    return turn, tools


@pytest.mark.parametrize("count", [1, 2])
async def test_all_rejected_closes_feedback_then_corrected_call(tmp_path, forced_tool, count):
    rejected = rejected_step()
    rejected[1:2] = [
        ToolCallRejected(call_id=f"rejected-{i}", argument_chars=2) for i in range(count)
    ]
    provider = ScriptedProvider([rejected, tool_step("test.read"), answer()])
    turn, tools = await run_required(tmp_path, provider, forced_tool)
    assert turn.status == TurnStatus.COMPLETED, turn.error
    assert turn.model_steps == len(provider.requests) == 3
    assert [request.tool_choice for request in provider.requests] == [
        "required",
        "required",
        "auto",
    ]
    assert provider.requests[1].forced_tool == forced_tool
    history = provider.requests[1].history
    facts = [item for item in history if isinstance(item.content, ToolCallRejectionContent)]
    results = [item for item in history if isinstance(item.content, ToolResultContent)]
    assert len(facts) == len(results) == count
    assert all(
        item.status == ItemStatus.COMPLETED and item.error is None for item in facts + results
    )
    assert [item.content for item in results] == [
        rejection_result(item.content.call_id) for item in facts
    ]
    assert all(item.content.model_step == 1 for item in facts)
    assert not any(isinstance(item.content, ToolCallContent) for item in history)
    calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    assert tools.calls == calls and len(calls) == 1 and calls[0].tool == "test.read"
    assert calls[0].call_id not in {item.content.call_id for item in facts}


@pytest.mark.parametrize("unknown_first", [False, True])
async def test_mixed_group_executes_only_required_tool(tmp_path, forced_tool, unknown_first):
    events = tool_step("test.read")
    events.insert(1 if unknown_first else 2, ToolCallRejected(call_id="unknown", argument_chars=2))
    provider = ScriptedProvider([events, answer()])
    turn, tools = await run_required(tmp_path, provider, forced_tool)
    assert turn.status == TurnStatus.COMPLETED and len(tools.calls) == 1
    assert tools.calls[0].tool == "test.read"


@pytest.mark.parametrize("with_rejection", [False, True])
@pytest.mark.parametrize("wrong_tool", ["test.other", "not.registered"])
async def test_forced_accepted_wrong_tool_still_fails(tmp_path, with_rejection, wrong_tool):
    class TwoTools(RecordingTools):
        def definitions(self):
            read = super().definitions()[0]
            return read, read.model_copy(update={"name": "test.other"})

    events = tool_step("test.read", wrong_tool)
    if with_rejection:
        events.insert(1, ToolCallRejected(call_id="unknown", argument_chars=2))
    provider = ScriptedProvider([events, tool_step("test.read")])
    turn, tools = await run_required(tmp_path, provider, "test.read", tools=TwoTools())
    assert {tool.name for tool in provider.requests[0].tools} == {"test.read", "test.other"}
    assert turn.status == TurnStatus.FAILED and turn.error.code == "invalid_provider_output"
    assert len(provider.requests) == 1 and tools.calls == []
    assert not any(
        isinstance(item.content, ToolCallContent | ToolCallRejectionContent) for item in turn.items
    )


@pytest.mark.parametrize(
    "case,code",
    [
        ("text", "invalid_provider_output"),
        ("empty", "invalid_provider_output"),
        ("malformed", "provider_invalid_provider_output"),
        ("duplicate", "invalid_provider_output"),
        ("after-terminal", "invalid_provider_output"),
        ("wrong-finish", "invalid_provider_output"),
        ("incomplete", "provider_stream_incomplete"),
        ("call-limit", "tool_call_limit"),
        ("output-limit", "model_output_too_large"),
    ],
)
async def test_invalid_required_group_publishes_no_proposals(tmp_path, forced_tool, case, code):
    events = rejected_step()
    budget = Budget()
    if case == "text":
        events = answer()
    elif case == "empty":
        events = [ResponseStarted(response_id="response"), ResponseCompleted()]
    elif case == "malformed":
        events = [
            ResponseStarted(response_id="response"),
            ResponseFailed(code="invalid_provider_output"),
        ]
    elif case == "duplicate":
        events.insert(2, ToolCallCompleted(call_id="rejected", tool="test.read"))
    elif case == "after-terminal":
        events.append(ToolCallRejected(call_id="late", argument_chars=2))
    elif case == "wrong-finish":
        events[-1] = ResponseCompleted()
    elif case == "incomplete":
        events.pop()
    elif case == "call-limit":
        events.insert(2, ToolCallRejected(call_id="second", argument_chars=2))
        budget = Budget(max_tool_calls_per_step=1)
    elif case == "output-limit":
        events = rejected_step(chars=1000)
        budget = Budget(max_output_chars=500)
    provider = ScriptedProvider([events, tool_step("test.read")])
    turn, tools = await run_required(tmp_path, provider, forced_tool, budget=budget)
    assert turn.status == TurnStatus.FAILED and turn.error.code == code
    assert len(provider.requests) == 1 and tools.calls == []
    assert not any(
        isinstance(item.content, ToolCallContent | ToolCallRejectionContent) for item in turn.items
    )


@pytest.mark.parametrize("limit", [1, 2])
async def test_required_rejections_do_not_reset_step_budget(tmp_path, forced_tool, limit):
    provider = ScriptedProvider([rejected_step()] * 3)
    turn, tools = await run_required(
        tmp_path, provider, forced_tool, budget=Budget(max_steps=limit)
    )
    assert turn.status == TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.model_steps == len(provider.requests) == limit and tools.calls == []
    assert len([i for i in turn.items if isinstance(i.content, ToolCallRejectionContent)]) == limit


@pytest.mark.parametrize("tokens", [50_000, 50_001])
async def test_rejection_usage_exhaustion_prevents_followup(tmp_path, forced_tool, tokens):
    events = rejected_step()
    events[-1] = ResponseCompleted(finish_reason="tool_calls", usage=Usage(input_tokens=tokens))
    provider = ScriptedProvider([events, tool_step("test.read")])
    turn, tools = await run_required(
        tmp_path, provider, forced_tool, budget=Budget(max_tokens=50_000, max_output_chars=1024)
    )
    assert turn.status == TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.usage.input_tokens == tokens and len(provider.requests) == 1 and tools.calls == []


@pytest.mark.parametrize("reported", [12, 13])
async def test_rejection_attempt_usage_is_preserved_and_mismatch_fails(
    tmp_path, forced_tool, reported
):
    start = attempt_start()
    events = [
        start,
        ResponseStarted(response_id="response-1"),
        ToolCallRejected(call_id="rejected", argument_chars=2),
        observed(start, completeness="complete", input_tokens=10, output_tokens=2),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=reported - 10)
        ),
    ]
    provider = ScriptedProvider([events, tool_step("test.read"), answer()])
    turn, tools = await run_required(tmp_path, provider, forced_tool)
    assert turn.usage == Usage(input_tokens=10, output_tokens=2)
    assert turn.model_attempts[0].status == "completed"
    if reported == 12:
        assert turn.status == TurnStatus.COMPLETED and len(provider.requests) == 3
        assert len(tools.calls) == 1
    else:
        assert turn.status == TurnStatus.FAILED and turn.error.code == "invalid_provider_output"
        assert len(provider.requests) == 1 and tools.calls == []
        assert not any(isinstance(i.content, ToolCallRejectionContent) for i in turn.items)


@pytest.mark.parametrize("closed", [False, True], ids=["before-close", "after-close"])
async def test_cancellation_preserves_only_closed_rejections(tmp_path, forced_tool, closed):
    class PausingProvider(ScriptedProvider):
        waiting = asyncio.Event()

        async def stream(self, request, cancel):
            async with aclosing(super().stream(request, cancel)) as stream:
                async for event in stream:
                    yield event
                    if (closed and request.step == 2 and isinstance(event, ResponseStarted)) or (
                        not closed and isinstance(event, ToolCallRejected)
                    ):
                        self.waiting.set()
                        await asyncio.Event().wait()

    provider = PausingProvider([rejected_step(), tool_step("test.read")])
    tools = RecordingTools()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, provider, tools, model_tool_view=RequiredUntilAccepted(forced_tool)
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "取消纠正", request_id="r"))
        try:
            await asyncio.wait_for(provider.waiting.wait(), 3)
            active = (await store.get_thread(thread.thread_id)).active_turn_id
            await runtime.cancel(thread.thread_id, active)
            turn = await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert turn.status == TurnStatus.CANCELLED and tools.calls == []
    assert provider.closed_streams == len(provider.requests) == (2 if closed else 1)
    assert len([i for i in turn.items if isinstance(i.content, ToolCallRejectionContent)]) == int(
        closed
    )
    snapshot = await store.get_thread(thread.thread_id)
    require_closed_rejections(snapshot)
    assert replay(await store.events(thread.thread_id)) == snapshot
