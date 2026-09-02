from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    Budget,
    ItemDelta,
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass, TraceContext
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


def assert_settled(turn: Turn) -> None:
    assert turn.status in TERMINAL_TURNS
    assert not pending_calls(turn)
    assert all(i.status != ItemStatus.STARTED for i in turn.items)


async def test_text_deltas_idempotency_and_trace_context(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = FakeProvider("答案")
    deltas: list[ItemDelta] = []
    trace = TraceContext(traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01")
    async with AgentRuntime(store, provider, on_delta=deltas.append) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r", trace_context=trace)
        assert turn.status == TurnStatus.COMPLETED
        assert_settled(turn)
        assert await runtime.run_turn(thread.thread_id, "任务", request_id="r") == turn
        with pytest.raises(KernelError, match="不同输入"):
            await runtime.run_turn(thread.thread_id, "其他任务", request_id="r")
        context = await runtime.action_context(thread.thread_id, turn.turn_id)
        assert context.session_id == str(thread.thread_id)
        assert context.run_id == str(turn.turn_id)
        assert context.trace_id == "a" * 32
        await runtime.cancel(thread.thread_id, turn.turn_id)
    assert len(provider.requests) == 1
    assert provider.closed_streams == 1
    assert [delta.delta for delta in deltas] == ["答案"]
    events = await store.events(thread.thread_id)
    assert "delta" not in {e.payload.type for e in events}
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_multiple_steps_and_calls_are_persisted_before_execution(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    thread_id: UUID

    class InspectingTools(RecordingTools):
        async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
            snapshot = await store.get_thread(thread_id)
            assert snapshot.active_turn_id is not None
            turn = snapshot.turns[-1]
            assert turn.status == TurnStatus.EXECUTING_TOOLS
            assert call in pending_calls(turn)
            return await super().execute(call, cancel)

    tools = InspectingTools()
    provider = ScriptedProvider(
        [
            tool_step("test.read", "test.read"),
            tool_step("test.read"),
            answer("验证完成"),
        ]
    )
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        thread_id = thread.thread_id
        turn = await runtime.run_turn(thread_id, "任务", request_id="r")
    assert turn.status == TurnStatus.COMPLETED
    assert turn.model_steps == 3
    assert len(tools.calls) == 3
    assert_settled(turn)
    assert sum(isinstance(i.content, ToolResultContent) for i in provider.requests[1].history) == 2
    assert sum(isinstance(i.content, ToolResultContent) for i in provider.requests[2].history) == 3
    assert len({c.call_id for c in tools.calls}) == 3


@pytest.mark.parametrize(
    ("effect", "approval", "tool_name", "code"),
    [
        (EffectClass.READ_ONLY, False, "missing", "unknown_tool"),
        (EffectClass.IDEMPOTENT_WRITE, False, "test.read", "tool_not_enabled"),
        (EffectClass.READ_ONLY, True, "test.read", "tool_not_enabled"),
    ],
)
async def test_unregistered_write_and_approval_tools_never_execute(
    tmp_path: Path, effect: EffectClass, approval: bool, tool_name: str, code: str
) -> None:
    tools = RecordingTools(effect=effect, approval=approval)
    provider = ScriptedProvider([tool_step(tool_name), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert tools.calls == []
    results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert results[0].error is not None
    assert results[0].error.code == code
    assert turn.status == TurnStatus.COMPLETED


@pytest.mark.parametrize("max_steps", [1, 2])
async def test_step_budget(tmp_path: Path, max_steps: int) -> None:
    provider = ScriptedProvider([tool_step("test.read")] * 3)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, RecordingTools()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "任务", request_id="r", budget=Budget(max_steps=max_steps)
        )
    assert turn.error is not None and turn.error.code == "budget_exceeded"
    assert len(provider.requests) == max_steps
    assert_settled(turn)


async def test_token_budget_prevents_tool_dispatch(tmp_path: Path) -> None:
    steps = tool_step("test.read")
    steps[-1] = ResponseCompleted(finish_reason="tool_calls", usage=Usage(input_tokens=100))
    provider = ScriptedProvider([steps])
    tools = RecordingTools()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "任务", request_id="r", budget=Budget(max_tokens=100)
        )
    assert tools.calls == []
    assert turn.usage.input_tokens == 100
    assert turn.error is not None and turn.error.code == "budget_exceeded"
    assert_settled(turn)


@pytest.mark.parametrize(
    "events",
    [
        [],
        [TextDelta(content_id="x", delta="x")],
        [ResponseStarted(response_id="x"), ResponseStarted(response_id="y")],
        [ResponseStarted(response_id="x"), TextDelta(content_id="x", delta="x")],
        [ResponseStarted(response_id="x"), TextStarted(content_id="x"), ResponseCompleted()],
        [*answer(), ResponseCompleted()],
        [
            ResponseStarted(response_id="x"),
            ToolCallCompleted(call_id="c", tool="test.read"),
            ToolCallCompleted(call_id="c", tool="test.read"),
            ResponseCompleted(finish_reason="tool_calls"),
        ],
        [
            ResponseStarted(response_id="x"),
            ToolCallCompleted(call_id="c", tool="test.read"),
            ResponseCompleted(),
        ],
        [
            ResponseStarted(response_id="x"),
            TextStarted(content_id="x"),
            TextDelta(content_id="x", delta="a"),
            TextCompleted(content_id="x", text="b"),
            ResponseCompleted(),
        ],
    ],
)
async def test_invalid_provider_stream_never_dispatches_tools(
    tmp_path: Path, events: list[ProviderEvent]
) -> None:
    provider = ScriptedProvider([events])
    tools = RecordingTools()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert turn.status == TurnStatus.FAILED
    assert tools.calls == []
    assert_settled(turn)
    assert provider.closed_streams == 1


async def test_output_limit(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider("12345")) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "任务", request_id="r", budget=Budget(max_output_chars=4)
        )
    assert turn.error is not None and turn.error.code == "model_output_too_large"
    assert_settled(turn)


class WaitingProvider:
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        try:
            yield ResponseStarted(response_id="r")
            yield TextStarted(content_id="answer")
            self.waiting.set()
            await asyncio.Event().wait()
        finally:
            self.closed.set()


async def test_user_cancel_during_provider_and_active_turn_conflict(tmp_path: Path) -> None:
    provider = WaitingProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
        await asyncio.wait_for(provider.waiting.wait(), 3)
        pending = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        with pytest.raises(KernelError, match="活跃 Turn"):
            await runtime.run_turn(thread.thread_id, "另一个任务", request_id="other")
        await runtime.cancel(thread.thread_id, pending.turn_id)
        turn = await asyncio.wait_for(task, 3)
        await runtime.cancel(thread.thread_id, turn.turn_id)
    assert turn.status == TurnStatus.CANCELLED
    assert provider.closed.is_set()
    assert_settled(turn)


async def test_task_cancel_persists_terminal_and_cleans_stream(tmp_path: Path) -> None:
    provider = WaitingProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
        await asyncio.wait_for(provider.waiting.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        snapshot = await store.get_thread(thread.thread_id)
    assert snapshot.turns[-1].status == TurnStatus.CANCELLED
    assert provider.closed.is_set()
    assert_settled(snapshot.turns[-1])


async def test_time_budget_closes_stream(tmp_path: Path) -> None:
    provider = WaitingProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "任务", request_id="r", budget=Budget(timeout_seconds=0.1)
        )
    assert turn.error is not None and turn.error.code == "time_budget_exceeded"
    assert provider.closed.is_set()
    assert_settled(turn)


async def test_cancel_during_tool_stops_follow_up_model(tmp_path: Path) -> None:
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    class WaitingTools(RecordingTools):
        async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            raise AssertionError("不可到达")

    provider = ScriptedProvider([tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, WaitingTools()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
        await asyncio.wait_for(entered.wait(), 3)
        active = (await store.get_thread(thread.thread_id)).active_turn_id
        assert active is not None
        await runtime.cancel(thread.thread_id, active)
        turn = await asyncio.wait_for(task, 3)
    assert turn.status == TurnStatus.CANCELLED
    assert len(provider.requests) == 1
    assert cleaned.is_set()
    assert_settled(turn)


async def test_only_one_runtime_host_and_lock_released(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()):
        with pytest.raises(KernelError, match="活跃 Runtime"):
            async with AgentRuntime(SQLiteSessionStore(store.path), FakeProvider()):
                raise AssertionError("不应取得宿主锁")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        assert (
            await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        ).status == "completed"


async def test_shutdown_cancels_managed_turn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = WaitingProvider()
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
        await asyncio.wait_for(provider.waiting.wait(), 3)
    assert task.done()
    assert task.result().status == TurnStatus.CANCELLED
    assert provider.closed.is_set()


async def test_raw_exception_not_persisted(tmp_path: Path) -> None:
    class BrokenTools(RecordingTools):
        async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
            raise RuntimeError("SYNTHETIC_SECRET_CANARY")

    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([tool_step("test.read")])
    async with AgentRuntime(store, provider, BrokenTools()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert turn.error is not None and turn.error.code == "runtime_error"
    assert "SYNTHETIC_SECRET_CANARY" not in turn.model_dump_json()
    assert all(
        "SYNTHETIC_SECRET_CANARY" not in event.model_dump_json()
        for event in await store.events(thread.thread_id)
    )
    assert_settled(turn)


async def test_provider_failure_is_classified_without_retry(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [[ResponseStarted(response_id="r"), ResponseFailed(code="rate_limit", retryable=True)]]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert turn.error is not None and turn.error.code == "provider_rate_limit"
    assert len(provider.requests) == 1
    assert_settled(turn)


async def test_followup_turn_loads_completed_history(tmp_path: Path) -> None:
    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "第一轮", request_id="r1")
        await runtime.run_turn(thread.thread_id, "第二轮", request_id="r2")
    assert [
        i.content.text for i in provider.requests[-1].history if isinstance(i.content, TextContent)
    ] == ["第一轮", "已完成", "第二轮"]


@pytest.mark.parametrize("outcome", ["wrong_id", "unknown"])
async def test_untrusted_tool_result_stops_loop(tmp_path: Path, outcome: str) -> None:
    from harnessix.agent.ids import new_id

    class BadResultTools(RecordingTools):
        async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
            return ToolResultContent(
                call_id=new_id() if outcome == "wrong_id" else call.call_id,
                outcome="succeeded" if outcome == "wrong_id" else "unknown",
            )

    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(store, provider, BadResultTools()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert len(provider.requests) == 1
    assert turn.status == (TurnStatus.FAILED if outcome == "wrong_id" else TurnStatus.INTERRUPTED)
    assert_settled(turn)


async def test_authentication_failure_before_response_started(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([[ResponseFailed(code="authentication")]])
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
    assert turn.error is not None and turn.error.code == "provider_authentication"
    assert_settled(turn)


async def test_cancellation_at_admission_commit_does_not_leave_active_turn(tmp_path: Path) -> None:
    armed = False

    def cancel_once(point: str) -> None:
        nonlocal armed
        if armed and point == "session.after_projection":
            armed = False
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    store = SQLiteSessionStore(tmp_path / "session.db", fault=cancel_once)
    provider = FakeProvider()
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        armed = True
        task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
        with pytest.raises(asyncio.CancelledError):
            await task
        snapshot = await store.get_thread(thread.thread_id)
        assert snapshot.active_turn_id is None
        assert all(turn.status in TERMINAL_TURNS for turn in snapshot.turns)
    assert provider.requests == []
