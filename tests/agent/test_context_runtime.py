from __future__ import annotations

from pathlib import Path

from harnessix.agent.errors import FailureCategory, KernelError
from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context import ContextEngine, ContextFragment, ContextFragmentKind, ContextLimits
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


def engine(*fragments: ContextFragment, window: int = 32_768) -> ContextEngine:
    return ContextEngine(
        ContextLimits(
            context_window_tokens=window,
            reserved_output_tokens=1024 if window > 1024 else window - 1,
            provider_overhead_tokens=0,
            safety_margin_tokens=0,
        ),
        fragments,
    )


async def test_context_is_sent_persisted_inspectable_and_replayable(tmp_path: Path) -> None:
    canary = "context-body-not-in-inspection"
    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    planner = engine(
        ContextFragment(
            kind=ContextFragmentKind.RUNTIME_INSTRUCTION,
            source="runtime",
            content="运行时规则",
        ),
        ContextFragment(
            kind=ContextFragmentKind.PROJECT_INSTRUCTION,
            source="AGENTS.md",
            content=canary,
        ),
    )
    async with AgentRuntime(store, provider, context=planner) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="context")
        inspected = await runtime.inspect_context(thread.thread_id, turn.turn_id)
        assert inspected == turn.context_inspections[0]
        assert (
            await runtime.inspect_context(thread.thread_id, turn.turn_id, model_step=1) == inspected
        )
        try:
            await runtime.inspect_context(thread.thread_id, turn.turn_id, model_step=2)
        except KernelError as error:
            assert error.code == "context_not_found"
        else:
            raise AssertionError("不存在的模型步骤必须失败")
    assert turn.status is TurnStatus.COMPLETED
    assert provider.requests[0].instructions is not None
    assert canary in provider.requests[0].instructions
    assert canary not in turn.context_inspections[0].model_dump_json()
    events = await store.events(thread.thread_id)
    prepared = [event for event in events if event.payload.type == "context_prepared"]
    assert len(prepared) == 1 and prepared[0].schema_version == 18
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_each_model_step_has_one_context_record(tmp_path: Path) -> None:
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store,
        provider,
        RecordingTools(),
        context=engine(
            ContextFragment(
                kind=ContextFragmentKind.USER_INSTRUCTION,
                source="profile",
                content="保持简洁",
            )
        ),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "读取", request_id="two-steps")
    assert turn.status is TurnStatus.COMPLETED
    assert [record.model_step for record in turn.context_inspections] == [1, 2]
    assert len(provider.requests) == 2
    assert all(request.instructions for request in provider.requests)
    assert turn.context_inspections[1].history_tokens > turn.context_inspections[0].history_tokens


async def test_context_overflow_stops_before_provider_and_is_budget_failure(tmp_path: Path) -> None:
    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, context=engine(window=1024)) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "x" * 2048, request_id="overflow")
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None
    assert turn.error.code == "context_budget_exceeded"
    assert turn.error.category is FailureCategory.BUDGET
    assert not provider.requests and not turn.context_inspections


async def test_context_record_survives_failure_after_commit(tmp_path: Path) -> None:
    def fault(point: str) -> None:
        if point == "runtime.after_context_prepared":
            raise RuntimeError("private-context-failure")

    provider = FakeProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, context=engine(), fault=fault) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="commit-failure")
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None and turn.error.code == "runtime_error"
    assert len(turn.context_inspections) == 1
    assert not provider.requests
    assert "private-context-failure" not in turn.model_dump_json()
