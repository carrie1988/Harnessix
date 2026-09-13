from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    QuestionAnswerContent,
    QuestionRequestContent,
    TextContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.costs import CostReportV3, build_cost_report
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer


def question_step(*, arguments: dict[str, object] | None = None):
    return [
        ResponseStarted(response_id="question-response"),
        ToolCallCompleted(
            call_id="question-call",
            tool="ask_user",
            arguments=arguments
            if arguments is not None
            else {"question": "选择发布环境", "options": ["测试", "生产"]},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def question(turn):
    return next(
        item.content
        for item in turn.items
        if item.status == ItemStatus.COMPLETED and isinstance(item.content, QuestionRequestContent)
    )


async def test_question_wait_restart_answer_and_model_projection(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    first_provider = ScriptedProvider([question_step(), answer("已按选择继续")])
    async with AgentRuntime(
        SQLiteSessionStore(path), first_provider, enable_questions=True
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        waiting = await runtime.run_turn(thread.thread_id, "准备发布", request_id="question")
        request = question(waiting)
        assert waiting.status == TurnStatus.WAITING_INPUT
        assert isinstance(build_cost_report(waiting), CostReportV3)
        assert pending_calls(waiting)[0].call_id == request.call_id
        assert first_provider.requests[0].tools[-1].name == "ask_user"
        assert replay(
            await runtime.store.events(thread.thread_id)
        ) == await runtime.store.get_thread(thread.thread_id)

    parked_provider = ScriptedProvider([question_step(), answer("已按选择继续")])
    store = SQLiteSessionStore(path)
    async with AgentRuntime(store, parked_provider, enable_questions=True) as runtime:
        still_waiting = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert still_waiting == waiting
        await runtime.steer_turn(
            thread.thread_id,
            waiting.turn_id,
            "回答后再执行校验",
            request_id="question-steering",
        )

        answered = await runtime.reply_question(
            thread.thread_id,
            waiting.turn_id,
            request.question_id,
            answer="生产",
        )
        assert answered.status == TurnStatus.EXECUTING_TOOLS
        sequence = (await store.get_thread(thread.thread_id)).sequence
        assert (
            await runtime.reply_question(
                thread.thread_id,
                waiting.turn_id,
                request.question_id,
                answer="生产",
            )
            == answered
        )
        assert (await store.get_thread(thread.thread_id)).sequence == sequence
        with pytest.raises(KernelError) as conflict:
            await runtime.reply_question(
                thread.thread_id,
                waiting.turn_id,
                request.question_id,
                answer="测试",
            )
        assert conflict.value.code == "question_conflict"

    assert parked_provider.requests == []
    resumed_provider = ScriptedProvider([question_step(), answer("已按选择继续")])
    async with AgentRuntime(
        SQLiteSessionStore(path), resumed_provider, enable_questions=True
    ) as runtime:
        completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert completed.status == TurnStatus.COMPLETED
        assert not pending_calls(completed)
        assert len(resumed_provider.requests) == 1
        history = resumed_provider.requests[0].history
        assert any(
            isinstance(item.content, ToolResultContent)
            and item.content.call_id == request.call_id
            and item.content.output == {"answer": "生产"}
            for item in history
        )
        assert not any(isinstance(item.content, QuestionAnswerContent) for item in history)
        assert isinstance(history[-1].content, TextContent)
        assert history[-1].content.text == "回答后再执行校验"
        assert replay(
            await runtime.store.events(thread.thread_id)
        ) == await runtime.store.get_thread(thread.thread_id)


async def test_question_can_be_cancelled_and_late_answer_is_rejected(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, ScriptedProvider([question_step()]), enable_questions=True
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        waiting = await runtime.run_turn(thread.thread_id, "准备发布", request_id="cancel")
        request = question(waiting)
        cancelled = await runtime.cancel(thread.thread_id, waiting.turn_id)
        assert cancelled.status == TurnStatus.CANCELLED
        assert not pending_calls(cancelled)
        with pytest.raises(KernelError) as closed:
            await runtime.reply_question(
                thread.thread_id,
                waiting.turn_id,
                request.question_id,
                answer="生产",
            )
        assert closed.value.code == "question_closed"


async def test_question_wait_consumes_wall_clock_budget(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, ScriptedProvider([question_step()]), enable_questions=True
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        waiting = await runtime.run_turn(thread.thread_id, "准备发布", request_id="expired")
        request = question(waiting)
        monkeypatch.setattr(
            "harnessix.agent.approvals.utc_now",
            lambda: waiting.created_at + timedelta(seconds=waiting.budget.timeout_seconds + 1),
        )
        with pytest.raises(KernelError) as expired:
            await runtime.reply_question(
                thread.thread_id,
                waiting.turn_id,
                request.question_id,
                answer="生产",
            )
        assert expired.value.code == "question_expired"
        finished = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert finished.status == TurnStatus.FAILED
        assert finished.error is not None
        assert finished.error.code == "time_budget_exceeded"


async def test_invalid_question_arguments_become_failed_tool_result(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [question_step(arguments={"question": ""}), answer("参数错误已处理")]
    )
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, enable_questions=True
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "提问", request_id="invalid")
    assert completed.status == TurnStatus.COMPLETED
    result = next(
        item.content for item in completed.items if isinstance(item.content, ToolResultContent)
    )
    assert result.outcome == "failed"
    assert result.error is not None and result.error.code == "tool_invalid_arguments"
    assert len(provider.requests) == 2


async def test_running_turn_steering_is_durable_and_consumed_by_next_step(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([answer("第一轮"), answer("第二轮")], delay_seconds=0.02)
    store = SQLiteSessionStore(tmp_path / "session.db")
    deltas = []
    first_delta = asyncio.Event()

    def receive_delta(delta):
        deltas.append(delta)
        first_delta.set()

    async with AgentRuntime(store, provider, on_delta=receive_delta) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "初始任务", request_id="steer")
        )
        await first_delta.wait()
        current = await store.get_thread(thread.thread_id)
        turn = current.turns[-1]
        steered = await runtime.steer_turn(
            thread.thread_id,
            turn.turn_id,
            "补充约束",
            request_id="steer-1",
        )
        sequence = steered.items[-1].item_id
        duplicate = await runtime.steer_turn(
            thread.thread_id,
            turn.turn_id,
            "补充约束",
            request_id="steer-1",
        )
        assert duplicate.items[-1].item_id == sequence
        with pytest.raises(KernelError) as conflict:
            await runtime.steer_turn(
                thread.thread_id,
                turn.turn_id,
                "其他约束",
                request_id="steer-1",
            )
        assert conflict.value.code == "steering_conflict"
        completed = await running

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 2
    second_history = provider.requests[1].history
    messages = [
        item.content.text for item in second_history if isinstance(item.content, TextContent)
    ]
    assert messages == ["初始任务", "第一轮", "补充约束"]
    events = await store.events(thread.thread_id)
    assert any(getattr(event.payload, "reason", None) == "steering" for event in events)
    assert replay(events) == await store.get_thread(thread.thread_id)
    with pytest.raises(KernelError) as closed:
        async with AgentRuntime(SQLiteSessionStore(store.path), ScriptedProvider([])) as runtime:
            await runtime.steer_turn(
                thread.thread_id,
                completed.turn_id,
                "太晚",
                request_id="late",
            )
    assert closed.value.code == "steering_closed"


async def test_accepted_turn_steering_joins_first_model_history(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer("完成")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        accepted = await runtime.accept_turn(
            thread.thread_id,
            "初始任务",
            request_id="accepted",
        )
        steered = await runtime.steer_turn(
            thread.thread_id,
            accepted.turn_id,
            "补充约束",
            request_id="accepted-steer",
        )
        assert steered.status == TurnStatus.ACCEPTED
        completed = await runtime.resume_turn(thread.thread_id, accepted.turn_id)

    assert completed.status == TurnStatus.COMPLETED
    messages = [
        item.content.text
        for item in provider.requests[0].history
        if isinstance(item.content, TextContent)
    ]
    assert messages == ["初始任务", "补充约束"]


async def test_steering_during_history_verification_restarts_preparation(tmp_path: Path) -> None:
    class GatedHistoryRuntime(AgentRuntime):
        def __init__(self, *args, **kwargs) -> None:
            self.history_entered = asyncio.Event()
            self.release_history = asyncio.Event()
            super().__init__(*args, **kwargs)

        async def _verify_history_artifacts(self, thread, prepared, token) -> None:
            if not self.history_entered.is_set():
                self.history_entered.set()
                await token.run(self.release_history.wait())
            await super()._verify_history_artifacts(thread, prepared, token)

    provider = ScriptedProvider([answer("完成")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with GatedHistoryRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "初始任务", request_id="verify-race")
        )
        await runtime.history_entered.wait()
        current = await store.get_thread(thread.thread_id)
        await runtime.steer_turn(
            thread.thread_id,
            current.turns[-1].turn_id,
            "验证期间补充",
            request_id="verify-race-steer",
        )
        runtime.release_history.set()
        completed = await running

    assert completed.status == TurnStatus.COMPLETED
    messages = [
        item.content.text
        for item in provider.requests[0].history
        if isinstance(item.content, TextContent)
    ]
    assert messages == ["初始任务", "验证期间补充"]


async def test_steering_before_first_model_item_is_ordered_after_response(tmp_path: Path) -> None:
    class GatedProvider:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(
            self, request: ModelRequest, cancel: CancelToken
        ) -> AsyncGenerator[ProviderEvent, None]:
            self.requests.append(request.model_copy(deep=True))
            if request.step == 1:
                self.entered.set()
                await self.release.wait()
                events = answer("第一轮")
            else:
                events = answer("第二轮")
            for event in events:
                cancel.checkpoint()
                yield event

    provider = GatedProvider()
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        running = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "初始任务", request_id="early-steer")
        )
        await provider.entered.wait()
        current = await store.get_thread(thread.thread_id)
        await runtime.steer_turn(
            thread.thread_id,
            current.turns[-1].turn_id,
            "提前补充",
            request_id="early",
        )
        provider.release.set()
        completed = await running

    assert completed.status == TurnStatus.COMPLETED
    messages = [
        item.content.text
        for item in provider.requests[1].history
        if isinstance(item.content, TextContent)
    ]
    assert messages == ["初始任务", "第一轮", "提前补充"]


async def test_hard_exit_after_question_commit_recovers_waiting_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.db"
    async with AgentRuntime(SQLiteSessionStore(path), ScriptedProvider([])) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    code = r"""
import asyncio, os, sys
from uuid import UUID
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore

def crash(name):
    if name == "runtime.after_question_request":
        os._exit(88)

async def main():
    provider = ScriptedProvider([[
        ResponseStarted(response_id="question"),
        ToolCallCompleted(
            call_id="ask",
            tool="ask_user",
            arguments={"question": "选择环境", "options": ["测试", "生产"]},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]])
    async with AgentRuntime(
        SQLiteSessionStore(sys.argv[1]), provider, enable_questions=True, fault=crash
    ) as runtime:
        await runtime.run_turn(UUID(sys.argv[2]), "准备发布", request_id="hard-exit")

asyncio.run(main())
raise AssertionError("未到达提问提交后的硬退出点")
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(path), str(thread.thread_id)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 88, child.stderr
    store = SQLiteSessionStore(path)
    provider = ScriptedProvider([question_step(), answer("恢复完成")])
    async with AgentRuntime(store, provider, enable_questions=True) as runtime:
        current = await store.get_thread(thread.thread_id)
        waiting = current.turns[-1]
        assert waiting.status == TurnStatus.WAITING_INPUT
        request = question(waiting)
        await runtime.reply_question(
            thread.thread_id,
            waiting.turn_id,
            request.question_id,
            answer="生产",
        )
        completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
        assert completed.status == TurnStatus.COMPLETED
        assert len(provider.requests) == 1 and provider.requests[0].step == 2
