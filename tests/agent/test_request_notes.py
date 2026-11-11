"""请求只追加（ADR 0132）：预算与目录说明落为历史Item，请求等于上一请求加新内容。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.models import (
    Budget,
    Item,
    ItemStatus,
    TextContent,
    ToolCallContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.reducer import replay
from harnessix.agent.request_notes import (
    BudgetLevel,
    budget_level,
    budget_note,
    budget_note_item_id,
    catalog_note_item_id,
    request_note_events,
    request_note_ids,
    without_request_notes,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttempt, UsageObservation
from harnessix.domain.models import ToolDescriptor, utc_now
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import tool_alias
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.projection import ProductViewState, ProjectedItem
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import PublicItem, PublicTextContent
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.product_ui.test_tui_usability import _thread

HOST_NOTICE = "[Harnessix host notice] "
STABLE_TAIL = OpenAIChatConfig(model="m", prompt_cache="stable_tail")


def attempt(step: int, input_tokens: int, output_tokens: int = 0) -> ModelAttempt:
    now = utc_now()
    return ModelAttempt(
        attempt_id=uuid4(),
        step=step,
        index=1,
        provider="test",
        requested_model="m",
        actual_model="m",
        response_id="r",
        usage=UsageObservation(
            completeness="complete", input_tokens=input_tokens, output_tokens=output_tokens
        ),
        status="completed",
        started_at=now,
        finished_at=now,
    )


def turn(
    *,
    model_steps: int = 0,
    max_steps: int = 100,
    max_tokens: int = 1_000_000_000,
    used_tokens: int = 0,
    attempts: tuple[ModelAttempt, ...] = (),
    items: tuple[Item, ...] = (),
) -> Turn:
    return Turn(
        turn_id=uuid4(),
        request_id="request-notes",
        request_fingerprint="0" * 64,
        status=TurnStatus.PREPARING_CONTEXT,
        budget=Budget(max_steps=max_steps, max_tokens=max_tokens),
        items=(user("fix the bug"), *items),
        model_steps=model_steps,
        usage=Usage(input_tokens=used_tokens),
        model_attempts=attempts,
        created_at=utc_now(),
    )


def user(text: str, item_id: UUID | None = None) -> Item:
    return Item(
        item_id=item_id or uuid4(),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text=text),
    )


def with_events(base: Turn, events) -> Turn:
    """把说明事件落为Item，模拟Reducer对ItemFinished的投影。"""
    finished = [e.payload for e in events if e.payload.type == "item_finished"]
    added = tuple(user(p.content.text, p.item_id) for p in finished)
    return base.model_copy(update={"items": (*base.items, *added)})


def note_texts(events) -> list[str]:
    return [e.payload.content.text for e in events if e.payload.type == "item_finished"]


@pytest.mark.parametrize(
    ("max_steps", "model_steps", "expected"),
    [
        (100, 0, None),
        (100, 49, None),
        (100, 50, BudgetLevel.HALF),
        (100, 74, BudgetLevel.HALF),
        (100, 75, BudgetLevel.QUARTER),
        (100, 89, BudgetLevel.QUARTER),
        (100, 90, BudgetLevel.FINAL),
        (100, 99, BudgetLevel.FINAL),
        # 步数预算本来就小：“最后十次”要等到剩余不足四分之一才成立，不在刚开始就催促收尾。
        (16, 0, None),
        (16, 6, None),
        (16, 8, BudgetLevel.HALF),
        (16, 12, BudgetLevel.FINAL),
        (4, 0, None),
        (4, 2, BudgetLevel.HALF),
        (4, 3, BudgetLevel.FINAL),
    ],
)
def test_step_budget_levels(max_steps: int, model_steps: int, expected: BudgetLevel | None):
    assert budget_level(turn(max_steps=max_steps, model_steps=model_steps)) is expected


def test_failed_retries_count_against_the_remaining_calls():
    retried = turn(model_steps=49, attempts=(attempt(1, 10), attempt(1, 10), attempt(2, 10)))
    assert budget_level(turn(model_steps=49)) is None
    assert budget_level(retried) is BudgetLevel.HALF
    assert "50 of 100 model calls are left" in budget_note(retried, BudgetLevel.HALF)


def test_token_budget_crosses_the_same_levels_with_token_equivalents():
    """Token是先用完的那一维时，关口按累计Token判断；“最后十次”按上一请求的尺寸折算。"""

    def at(used: int, request_size: int = 100_000) -> Turn:
        return turn(
            model_steps=10,
            max_tokens=6_000_000,
            used_tokens=used,
            attempts=(attempt(10, request_size - 100, 100),),
        )

    assert budget_level(at(2_999_999)) is None
    assert budget_level(at(3_000_000)) is BudgetLevel.HALF
    # 剩四分之一（150万）时按10万一次还够15次：是四分之一关口，还不是最后十次。
    assert budget_level(at(4_500_000)) is BudgetLevel.QUARTER
    assert budget_level(at(5_000_000)) is BudgetLevel.FINAL
    # 请求很大时，剩四分之一就已经不够十次。
    assert budget_level(at(4_500_000, request_size=200_000)) is BudgetLevel.FINAL
    note = budget_note(at(5_000_000), BudgetLevel.FINAL)
    assert note.startswith(HOST_NOTICE + "Budget checkpoint: 90 of 100 model calls are left; ")
    assert "1,000,000 of 6,000,000 tokens are left, enough for about 10 more calls" in note
    assert "Stop exploring" in note and "final summary" in note


def test_uncapped_token_budgets_do_not_mention_tokens():
    """交互使用不设Token上限：说明只写步数。"""
    note = budget_note(turn(model_steps=50), BudgetLevel.HALF)
    assert note == (
        HOST_NOTICE + "Budget checkpoint: 50 of 100 model calls are left. "
        "About half of the budget is used. Decide what is still needed to finish and verify "
        "the work, and leave out the rest."
    )


def test_each_level_is_announced_once_and_only_tighter_levels_follow():
    current = turn(model_steps=49)
    assert request_note_events(current, None) == []
    seen: list[BudgetLevel] = []
    for model_steps in range(50, 100):
        current = current.model_copy(update={"model_steps": model_steps})
        events = request_note_events(current, None)
        for level in BudgetLevel:
            if any(
                e.payload.item_id == budget_note_item_id(current.turn_id, level) for e in events
            ):
                seen.append(level)
        current = with_events(current, events)
        # 同一快照重判不再发：恢复重入时不会重复提醒。
        assert request_note_events(current, None) == []
    assert seen == [BudgetLevel.HALF, BudgetLevel.QUARTER, BudgetLevel.FINAL]


def test_a_skipped_level_is_not_announced_later():
    """一步内从“不到一半”跳到“最后十次”时只提醒最紧的关口，之后不补发较松的。"""
    current = turn(model_steps=95)
    events = request_note_events(current, None)
    assert [e.payload.item_id for e in events if e.payload.type == "item_finished"] == [
        budget_note_item_id(current.turn_id, BudgetLevel.FINAL)
    ]
    assert request_note_events(with_events(current, events), None) == []


def test_catalog_notes_follow_changes_of_the_callable_set_within_the_turn():
    current = turn()
    assert request_note_events(current, None) == []
    sent: list[str] = []
    for index, text in enumerate(["tools A", "tools A", "tools B", "tools A"]):
        events = request_note_events(current, text)
        if index == 1:
            assert events == []
            continue
        (started, finished) = events
        assert finished.payload.item_id == catalog_note_item_id(current.turn_id, len(sent))
        assert finished.payload.content == TextContent(kind="user_message", text=text)
        assert finished.payload.status == ItemStatus.COMPLETED
        sent.append(text)
        current = with_events(current, events)
    assert sent == ["tools A", "tools B", "tools A"]


def test_request_notes_are_told_apart_from_user_input():
    base = turn(model_steps=50)
    steering = user("also update the docs")
    noted = with_events(base, request_note_events(base, "tools A"))
    noted = noted.model_copy(update={"items": (*noted.items, steering)})
    noted = with_events(noted, request_note_events(noted, "tools B"))
    ids = request_note_ids(noted.turn_id, (item.item_id for item in noted.items))
    assert ids == {
        budget_note_item_id(noted.turn_id, BudgetLevel.HALF),
        catalog_note_item_id(noted.turn_id, 0),
        catalog_note_item_id(noted.turn_id, 1),
    }
    visible = without_request_notes(noted)
    assert [item.content.text for item in visible.items] == ["fix the bug", "also update the docs"]
    assert visible.turn_id == noted.turn_id and len(noted.items) == 5


def wire_messages(request: ModelRequest) -> list[dict]:
    body, _ = build_request(request, STABLE_TAIL)
    return body["messages"]


async def run_tool_turn(tmp_path: Path, steps: int, **runtime_options) -> tuple:
    provider = ScriptedProvider([*([tool_step("test.read")] * (steps - 1)), answer("done")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, RecordingTools(), **runtime_options) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(
            thread.thread_id, "fix the bug", request_id="notes", budget=Budget(max_steps=steps)
        )
    assert completed.status == TurnStatus.COMPLETED and len(provider.requests) == steps
    return provider, store, thread, completed


async def test_requests_only_append_to_the_previous_request(tmp_path: Path):
    """gin-4003 md3MRob step 15的一般形态：只新增一条工具结果的请求，其余部分与上一请求逐条相同。

    末尾逐请求说明使服务端缓存停在上一条说明之前；说明进入历史后，每个请求都是上一请求的延伸。
    """
    provider, store, thread, completed = await run_tool_turn(tmp_path, 8, history_notes=True)

    assert all(request.notes_in_history for request in provider.requests)
    for previous, following in zip(provider.requests, provider.requests[1:], strict=False):
        assert following.history[: len(previous.history)] == previous.history
        before, after = wire_messages(previous), wire_messages(following)
        assert after[: len(before)] == before and len(after) > len(before)
    assert not any(
        str(message["content"]).startswith("[Harness note] ")
        for message in wire_messages(provider.requests[-1])
    )
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_budget_notes_become_history_at_the_checkpoints(tmp_path: Path):
    provider, _, _, completed = await run_tool_turn(tmp_path, 8, history_notes=True)

    def notes(request: ModelRequest) -> list[str]:
        return [
            item.content.text
            for item in request.history
            if isinstance(item.content, TextContent)
            and item.content.text.startswith(HOST_NOTICE + "Budget checkpoint")
        ]

    assert [len(notes(request)) for request in provider.requests] == [0, 0, 0, 0, 1, 1, 2, 2]
    half, final = notes(provider.requests[-1])
    assert "4 of 8 model calls are left" in half and "About half of the budget" in half
    assert "2 of 8 model calls are left" in final and "Stop exploring" in final
    # 说明排在上一步的工具结果之后、本步模型输出之前，并且是本Turn的持久Item。
    fifth = provider.requests[4].history
    assert fifth[-1].item_id == budget_note_item_id(completed.turn_id, BudgetLevel.HALF)
    assert fifth[-2].content.kind == "tool_result"
    assert {item.item_id for item in completed.items} >= {
        budget_note_item_id(completed.turn_id, BudgetLevel.HALF),
        budget_note_item_id(completed.turn_id, BudgetLevel.FINAL),
    }


async def test_without_the_switch_the_trailing_note_still_breaks_the_prefix(tmp_path: Path):
    """默认关闭：旧宿主沿用逐请求说明，stable_tail下上一请求的末尾说明不在下一请求里。"""
    provider, _, _, completed = await run_tool_turn(tmp_path, 4)

    assert not any(request.notes_in_history for request in provider.requests)
    before, after = wire_messages(provider.requests[1]), wire_messages(provider.requests[2])
    assert str(before[-1]["content"]).startswith("[Harness note] Current request tool catalog")
    assert after[: len(before)] != before
    assert not request_note_ids(completed.turn_id, (item.item_id for item in completed.items))


class PhasedView:
    """前两步全部工具可用，之后不再提供工具并说明原因；记录视图看到的用户消息。"""

    def __init__(self) -> None:
        self.user_texts: list[list[str]] = []

    def select(self, turn: Turn, definitions: tuple[ToolDescriptor, ...]) -> ModelToolSelection:
        self.user_texts.append(
            [
                item.content.text
                for item in turn.items
                if isinstance(item.content, TextContent) and item.content.kind == "user_message"
            ]
        )
        if turn.model_steps < 2:
            return ModelToolSelection(definitions)
        return ModelToolSelection((), notice="Reading is finished; answer now.")


async def test_catalog_notes_are_sent_when_a_tool_view_changes_the_callable_set(tmp_path: Path):
    view = PhasedView()
    provider, store, thread, completed = await run_tool_turn(
        tmp_path, 3, history_notes=True, model_tool_view=view
    )

    def catalog_notes(request: ModelRequest) -> list[str]:
        return [
            item.content.text
            for item in request.history
            if isinstance(item.content, TextContent)
            and item.content.text.startswith(HOST_NOTICE + "Callable tools from now on")
        ]

    first, second, third = (catalog_notes(request) for request in provider.requests)
    assert len(first) == 1 and second == first and len(third) == 2
    assert f'["{tool_alias("test.read")}"]' in first[0] and "Tool choice: auto" in first[0]
    assert "until a newer host notice replaces this list" in first[0]
    assert "(wire aliases): []" in third[1] and "No tools are callable." in third[1]
    assert "Why the catalog is restricted now: Reading is finished; answer now." in third[1]
    assert [
        item.item_id
        for item in completed.items
        if isinstance(item.content, TextContent) and item.content.text in third
    ] == [
        catalog_note_item_id(completed.turn_id, 0),
        catalog_note_item_id(completed.turn_id, 1),
    ]
    # 视图判断“最近一次用户输入”时看不到宿主说明。
    assert all(texts == ["fix the bug"] for texts in view.user_texts)
    for previous, following in zip(provider.requests, provider.requests[1:], strict=False):
        assert following.history[: len(previous.history)] == previous.history
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_a_turn_without_a_tool_view_gets_no_catalog_note(tmp_path: Path):
    provider, _, _, completed = await run_tool_turn(tmp_path, 3, history_notes=True)
    texts = [
        item.content.text
        for item in provider.requests[-1].history
        if isinstance(item.content, TextContent)
    ]
    assert not any("Callable tools" in text or "tool catalog" in text for text in texts)
    assert any(isinstance(item.content, ToolCallContent) for item in provider.requests[-1].history)


def test_request_notes_are_not_shown_in_the_transcript():
    """说明只给模型看：界面把用户消息显示为“你”，宿主说明不应冒充用户输入。"""
    thread = _thread()
    turn_id = thread.thread_id

    def entry(item_id: UUID, text: str) -> ProjectedItem:
        return ProjectedItem(
            item=PublicItem(
                item_id=item_id,
                status="completed",
                content=PublicTextContent(kind="user_message", text=text),
            ),
            turn_id=turn_id,
            first_cursor=1,
            last_cursor=1,
            final=True,
        )

    view = ProductViewState(
        thread=thread,
        durable_cursor=2,
        items=(
            entry(uuid4(), "fix the bug"),
            entry(catalog_note_item_id(turn_id, 0), HOST_NOTICE + "Callable tools from now on"),
            entry(
                budget_note_item_id(turn_id, BudgetLevel.HALF), HOST_NOTICE + "Budget checkpoint"
            ),
            entry(uuid4(), "also update the docs"),
            entry(catalog_note_item_id(turn_id, 1), HOST_NOTICE + "Callable tools from now on"),
        ),
    )
    assert [line.text for line in transcript_lines(view)] == ["fix the bug", "also update the docs"]
