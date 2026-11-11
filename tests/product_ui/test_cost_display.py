"""状态栏费用的准确性：标注“本轮”，中断的模型请求不把已报告部分当作上限（0.10.0b10）。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from harnessix.product_ui.cost_estimate import ListPrice
from harnessix.product_ui.main_view import _status_text
from harnessix.product_ui.projection import (
    ProductViewState,
    TurnProjection,
    apply_replay_page,
)
from harnessix.protocol.contracts import (
    EventsReplayResult,
    PublicEvent,
    PublicFailure,
    PublicUsage,
    TurnStatePublicEvent,
)
from tests.product_ui.test_tui_usability import NOW, _state, _thread

QWEN_FLASH = ListPrice("CNY", Decimal("0.8"), Decimal("2.7"))
THREAD = _thread()


def _view(status: str, *, usage=(5308, 175)) -> ProductViewState:
    turn = TurnProjection(
        turn_id=uuid4(),
        request_id="r",
        status=status,
        budget=None,
        usage=PublicUsage(
            input_tokens=usage[0], output_tokens=usage[1], total_tokens=usage[0] + usage[1]
        ),
        model_step=1,
        usage_cursor=1,
    )
    return ProductViewState(thread=THREAD, durable_cursor=1, current_turn=turn)


def _end(view: ProductViewState, status: str, code: str | None) -> ProductViewState:
    assert view.current_turn is not None
    event = PublicEvent(
        event_id=uuid4(),
        thread_id=THREAD.thread_id,
        turn_id=view.current_turn.turn_id,
        cursor=view.durable_cursor + 1,
        occurred_at=NOW,
        data=TurnStatePublicEvent(
            status=status,
            error=None
            if code is None
            else PublicFailure(code=code, message="x", category="provider"),
        ),
    )
    page = EventsReplayResult(
        thread_id=THREAD.thread_id,
        events=(event,),
        scanned_through=event.cursor,
        has_more=False,
    )
    return apply_replay_page(view, page)


def _status(view: ProductViewState) -> str:
    return _status_text(replace(_state(view, 1), list_price=QWEN_FLASH))


def test_a_completed_turn_shows_this_turns_tokens_and_a_true_upper_bound() -> None:
    # 2026-11-10用户会话实测：第1步5308输入+175输出，按0.8/2.7元每百万即0.004719元。
    done = _end(_view("finalizing"), "completed", None)
    assert " · 本轮 5,483 tokens · 费用≤¥0.0047" in _status(done)
    assert "另有中断" not in _status(done)


def test_a_model_call_that_times_out_is_not_presented_as_an_upper_bound() -> None:
    failed = _end(_view("calling_model"), "failed", "provider_transport")
    text = _status(failed)
    assert "已知费用≤¥0.0047 · 另有中断的请求可能已计费" in text


def test_cancelling_during_a_model_call_is_marked_too() -> None:
    cancelling = _end(_view("calling_model"), "cancelling", None)
    assert cancelling.current_turn is not None and cancelling.current_turn.interrupted_model_call


def test_known_unbilled_failures_and_tool_failures_keep_the_upper_bound() -> None:
    rejected = _end(_view("calling_model"), "failed", "provider_authentication")
    tool_failed = _end(_view("executing_tools"), "failed", "tool_failed")
    for view in (rejected, tool_failed):
        assert "另有中断" not in _status(view)
