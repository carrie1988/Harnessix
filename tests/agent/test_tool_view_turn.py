"""交给工具视图的Turn（ADR 0168）：只有声明只读的视图拿共享对象的浅副本，选择结果与深拷贝时相同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harnessix.agent import runtime as runtime_module
from harnessix.agent.models import Item, Turn
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.request_notes import (
    request_note_events,
    tool_view_turn,
    without_request_notes,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ToolDescriptor
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.wrap_up import WRAP_UP_STEER, WrapUpToolView
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_request_notes import turn, user, with_events


@dataclass
class _RecordingView:
    """记下select拿到的Turn；reads_turn_in_place按构造参数声明，None表示没有该属性。"""

    seen: list[Turn] = field(default_factory=list)

    def select(self, turn: Turn, definitions: tuple[ToolDescriptor, ...]) -> ModelToolSelection:
        self.seen.append(turn)
        return ModelToolSelection(definitions)


@dataclass
class _DeclaringView(_RecordingView):
    reads_turn_in_place: object = True


def _noted_turn() -> Turn:
    base = turn(model_steps=50, items=(user("also update the docs"),))
    return with_events(base, request_note_events(base, "tools A"))


def _same_objects(left: tuple[Item, ...], right: tuple[Item, ...]) -> bool:
    return len(left) == len(right) and all(a is b for a, b in zip(left, right, strict=True))


def test_only_a_view_declaring_true_gets_the_session_items_themselves() -> None:
    session_turn = _noted_turn()
    expected = without_request_notes(session_turn)
    for view, shared in (
        (_DeclaringView(), True),
        (_DeclaringView(reads_turn_in_place=False), False),
        # 只认布尔True：真值但不是True的声明不放宽。
        (_DeclaringView(reads_turn_in_place=1), False),
        (_RecordingView(), False),
    ):
        given = tool_view_turn(view, session_turn)
        # 内容总与深拷贝路径相同（说明照旧去掉），差别只在是否共享对象。
        assert given == expected
        assert given.model_dump_json() == expected.model_dump_json()
        assert _same_objects(given.items, session_turn.items[: len(given.items)]) is shared
        assert given is not session_turn


def test_the_wrap_up_view_declares_in_place_reading_only_without_an_inner_view() -> None:
    assert WrapUpToolView().reads_turn_in_place is True
    assert WrapUpToolView(_RecordingView()).reads_turn_in_place is False
    inner = _RecordingView()
    session_turn = _noted_turn()
    WrapUpToolView(inner).select(tool_view_turn(WrapUpToolView(inner), session_turn), ())
    # 内层视图仍拿到独立的深拷贝。
    assert not any(a is b for a, b in zip(inner.seen[0].items, session_turn.items, strict=False))


@pytest.mark.parametrize("wrap_up", [False, True])
def test_wrap_up_selection_is_identical_on_shared_and_copied_turns(wrap_up: bool) -> None:
    session_turn = _noted_turn()
    if wrap_up:
        session_turn = session_turn.model_copy(
            update={"items": (*session_turn.items, user(WRAP_UP_STEER))}
        )
    before = session_turn.model_dump_json()
    view = WrapUpToolView()
    definitions = RecordingTools().definitions()
    shared = view.select(tool_view_turn(view, session_turn), definitions)
    copied = view.select(without_request_notes(session_turn), definitions)
    assert shared == copied
    assert (shared.tools == ()) is wrap_up
    assert session_turn.model_dump_json() == before


async def test_every_selection_of_a_product_turn_equals_the_deep_copy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次选择都再用深拷贝的Turn选一遍并逐项比对：共享路径不改变任何一步的工具目录。"""
    original = AgentRuntime._select_model_tools
    compared: list[bool] = []

    def both(self: AgentRuntime, turn: Turn, available: tuple[ToolDescriptor, ...]):  # type: ignore[no-untyped-def]
        shared = original(self, turn, available)
        with monkeypatch.context() as patch:
            patch.setattr(
                runtime_module, "tool_view_turn", lambda _view, t: without_request_notes(t)
            )
            copied = original(self, turn, available)
        compared.append(shared == copied)
        return shared

    monkeypatch.setattr(AgentRuntime, "_select_model_tools", both)
    provider = ScriptedProvider([tool_step("test.read"), tool_step("test.read"), answer("done")])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        provider,
        RecordingTools(),
        model_tool_view=WrapUpToolView(),
        history_notes=True,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        finished = await runtime.run_turn(thread.thread_id, "read twice", request_id="r")
    assert finished.status.value == "completed", finished.error
    # 首步的目录说明与每步的工具选择各比对一次。
    assert len(compared) >= 3 and all(compared)
