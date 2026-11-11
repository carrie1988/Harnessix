"""工具步骤说明：一步的工具结果全部落账后，来源给出的事实以宿主说明进入持久历史，且有界、不重复。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.models import (
    Budget,
    Item,
    ItemStatus,
    TextContent,
    Thread,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.step_notices import (
    MAX_STEP_NOTICE_CHARS,
    MAX_STEP_NOTICES_PER_TURN,
    step_notice_item_id,
    step_notice_text,
)
from harnessix.domain.models import utc_now
from harnessix.models._history import messages_for
from harnessix.models.contracts import (
    ModelRequest,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step

PREFIX = "[Harnessix host notice] "


class FixedSource:
    """每次被询问都给出同一条事实，并记录被询问时Turn所在的步骤。"""

    def __init__(self, fact: str | None = "the check now fails") -> None:
        self.fact = fact
        self.asked: list[int] = []

    def step_notice(self, thread: Thread, turn: Turn) -> str | None:
        assert turn in thread.turns
        self.asked.append(turn.model_steps)
        return self.fact


def _turn(
    *notice_steps: int, model_steps: int, status: TurnStatus = TurnStatus.EXECUTING_TOOLS
) -> tuple[Thread, Turn]:
    """只含用户输入与指定步骤已发布说明的Turn，停在第model_steps步的工具结果之后。"""
    turn_id = uuid4()
    items = [
        Item(
            item_id=item_id,
            status=ItemStatus.COMPLETED,
            content=TextContent(kind="user_message", text=text),
        )
        for item_id, text in [
            (uuid4(), "fix the bug"),
            *((step_notice_item_id(turn_id, step), PREFIX + "earlier") for step in notice_steps),
        ]
    ]
    turn = Turn(
        turn_id=turn_id,
        request_id="step-notice",
        request_fingerprint="0" * 64,
        status=status,
        budget=Budget(max_steps=100),
        items=tuple(items),
        model_steps=model_steps,
        created_at=utc_now(),
    )
    now = utc_now()
    thread = Thread(
        thread_id=uuid4(), workspace="/w", turns=(turn,), created_at=now, updated_at=now
    )
    return thread, turn


def test_the_notice_is_the_sources_fact_behind_the_host_prefix() -> None:
    thread, turn = _turn(model_steps=3)

    assert step_notice_text(FixedSource(), thread, turn) == PREFIX + "the check now fails"


@pytest.mark.parametrize("fact", [None, ""])
def test_no_fact_means_no_notice(fact: str | None) -> None:
    thread, turn = _turn(model_steps=3)

    assert step_notice_text(FixedSource(fact), thread, turn) is None


def test_a_step_is_noticed_at_most_once() -> None:
    """恢复重入同一步时，已发布的说明不再重复，也不再询问来源。"""
    thread, turn = _turn(3, model_steps=3)
    source = FixedSource()

    assert step_notice_text(source, thread, turn) is None
    assert source.asked == []


def test_notices_stop_at_the_per_turn_cap() -> None:
    below = tuple(range(1, MAX_STEP_NOTICES_PER_TURN))
    thread, turn = _turn(*below, model_steps=20)
    assert step_notice_text(FixedSource(), thread, turn) is not None

    thread, turn = _turn(*below, MAX_STEP_NOTICES_PER_TURN, model_steps=20)
    assert step_notice_text(FixedSource(), thread, turn) is None


def test_a_notice_is_bounded_in_size() -> None:
    thread, turn = _turn(model_steps=1)

    text = step_notice_text(FixedSource("x" * 100_000), thread, turn)

    assert text is not None and len(text) == MAX_STEP_NOTICE_CHARS


def test_a_turn_that_is_closing_gets_no_notice() -> None:
    thread, turn = _turn(model_steps=2, status=TurnStatus.CANCELLING)

    assert step_notice_text(FixedSource(), thread, turn) is None


def _history(request: ModelRequest) -> list[tuple[str, str]]:
    """请求历史里每个Item的种类；文本Item附正文。"""
    return [
        (entry.content.kind, entry.content.text if isinstance(entry.content, TextContent) else "")
        for entry in request.history
    ]


async def test_runtime_appends_the_notice_after_the_tool_results(tmp_path: Path) -> None:
    provider = ScriptedProvider([tool_step("test.read"), answer("done")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    source = FixedSource()
    async with AgentRuntime(store, provider, RecordingTools(), step_notices=source) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="notice")

    assert completed.status == TurnStatus.COMPLETED
    assert source.asked == [1]
    assert _history(provider.requests[1]) == [
        ("user_message", "fix the bug"),
        ("tool_call", ""),
        ("tool_result", ""),
        ("user_message", PREFIX + "the check now fails"),
    ]
    # Provider线协议接受“工具结果之后的用户消息”，与运行中Steering相同。
    assert [message["role"] for message in messages_for(provider.requests[1])] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    notice = completed.items[3]
    assert notice.item_id == step_notice_item_id(completed.turn_id, 1)
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_runtime_places_one_notice_after_all_results_of_the_step(tmp_path: Path) -> None:
    provider = ScriptedProvider([tool_step("test.read", "test.read"), answer("done")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    source = FixedSource()
    async with AgentRuntime(store, provider, RecordingTools(), step_notices=source) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "fix the bug", request_id="group")

    assert source.asked == [1]
    assert [kind for kind, _ in _history(provider.requests[1])] == [
        "user_message",
        "tool_call",
        "tool_call",
        "tool_result",
        "tool_result",
        "user_message",
    ]
    assert messages_for(provider.requests[1])[-1]["content"] == PREFIX + "the check now fails"


async def test_runtime_notices_a_step_that_ends_with_a_plan_update(tmp_path: Path) -> None:
    """计划更新把Turn提前切到准备阶段；说明仍落在该步结果之后。"""
    plan = {"steps": [{"step_id": "1", "description": "Fix the bug", "status": "in_progress"}]}
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="response"),
                ToolCallCompleted(call_id="read", tool="test.read", arguments={}),
                ToolCallCompleted(call_id="plan", tool="update_plan", arguments=plan),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("done"),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, provider, RecordingTools(), enable_planning=True, step_notices=FixedSource()
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="plan")

    assert completed.status == TurnStatus.COMPLETED
    assert _history(provider.requests[1])[-1] == ("user_message", PREFIX + "the check now fails")
    assert all(
        result.outcome == "succeeded"
        for result in (item.content for item in completed.items)
        if isinstance(result, ToolResultContent)
    )


async def test_runtime_without_a_fact_or_a_source_adds_nothing(tmp_path: Path) -> None:
    for name, source in (("silent", FixedSource(None)), ("absent", None)):
        provider = ScriptedProvider([tool_step("test.read"), answer("done")])
        store = SQLiteSessionStore(tmp_path / f"{name}.db")
        async with AgentRuntime(store, provider, RecordingTools(), step_notices=source) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id=name)

        assert completed.status == TurnStatus.COMPLETED
        assert [kind for kind, _ in _history(provider.requests[1])] == [
            "user_message",
            "tool_call",
            "tool_result",
        ]


async def test_runtime_does_not_notice_a_reply_without_tool_calls(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer("nothing to do")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    source = FixedSource()
    async with AgentRuntime(store, provider, RecordingTools(), step_notices=source) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "hello", request_id="text")

    assert completed.status == TurnStatus.COMPLETED
    assert source.asked == []
