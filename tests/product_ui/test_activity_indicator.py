"""任务运行时的动态指示：转动、计时，只在Agent自己运行时显示（0.10.0b8）。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from textual.app import App, ComposeResult

from harnessix.product_ui.activity_indicator import ActivityIndicator, activity_text
from harnessix.product_ui.projection import ProductViewState, TransientItemStream, TurnProjection
from tests.product_ui.test_tui_usability import _state, _thread


def test_activity_text_spins_and_counts() -> None:
    first = activity_text("calling_model", 5.4, 0, streaming=False)
    second = activity_text("calling_model", 5.4, 1, streaming=False)
    assert first == "⠋ 思考中 · 已运行 5 秒"
    assert second.startswith("⠙ ") and first[1:] == second[1:]
    assert (
        activity_text("calling_model", 75, 0, streaming=True) == "⠋ 生成回复中 · 已运行 1 分 15 秒"
    )
    assert activity_text("executing_tools", 3, 0, streaming=False).startswith("⠋ 执行中")


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield ActivityIndicator()


def _view(status: str, *, streaming: bool = False) -> ProductViewState:
    thread = _thread()
    # 第1步正在调用模型时，已结算用量的步数仍是0。
    turn = TurnProjection(
        turn_id=uuid4(), request_id="r", status=status, budget=None, usage=None, model_step=0
    )
    streams = (
        (
            TransientItemStream(
                item_id=uuid4(), turn_id=turn.turn_id, model_step=1, last_sequence=1, text="正在"
            ),
        )
        if streaming
        else ()
    )
    return ProductViewState(thread=thread, durable_cursor=1, current_turn=turn, streams=streams)


async def test_indicator_moves_while_running_and_hides_when_waiting_or_done() -> None:
    app = _Host()
    async with app.run_test(size=(80, 10)) as pilot:
        indicator = app.query_one(ActivityIndicator)
        assert not indicator.display
        running = _view("calling_model")
        indicator.follow(_state(running, 1))
        await pilot.pause()
        shown = str(indicator.render())
        assert indicator.display and "思考中 · 已运行" in shown
        await pilot.pause(0.4)
        # 没有新事件时，转动符号仍在变化，表示任务仍在运行。
        assert str(indicator.render())[0] != shown[0]
        indicator.follow(_state(_view("calling_model", streaming=True), 2))
        assert "生成回复中" in str(indicator.render())
        waiting = replace(
            running, current_turn=replace(running.current_turn, status="waiting_approval")
        )
        indicator.follow(_state(waiting, 3))
        assert not indicator.display
        done = replace(running, current_turn=replace(running.current_turn, status="completed"))
        indicator.follow(_state(done, 4))
        assert not indicator.display


async def test_elapsed_time_continues_across_an_approval_in_the_same_turn(monkeypatch) -> None:
    """Beta反馈HX-08：批准后“已运行”从0重新计，连沙箱命令约40毫秒的自动批准也会清零。"""
    from harnessix.product_ui import activity_indicator

    clock = [100.0]
    monkeypatch.setattr(activity_indicator, "monotonic", lambda: clock[0])
    app = _Host()
    async with app.run_test(size=(80, 10)) as pilot:
        indicator = app.query_one(ActivityIndicator)
        running = _view("executing_tools")
        indicator.follow(_state(running, 1))
        clock[0] += 180
        waiting = replace(
            running, current_turn=replace(running.current_turn, status="waiting_approval")
        )
        indicator.follow(_state(waiting, 2))
        assert not indicator.display
        clock[0] += 20
        indicator.follow(_state(running, 3))
        await pilot.pause()
        # 同一轮：等待审批的时间也计入，与本轮的时间上限口径一致。
        assert "已运行 3 分 20 秒" in str(indicator.render())

        clock[0] += 5
        next_turn = _view("calling_model")
        indicator.follow(_state(next_turn, 4))
        await pilot.pause()
        assert "已运行 0 秒" in str(indicator.render())
