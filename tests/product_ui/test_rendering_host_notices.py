"""宿主补发给模型的说明不是用户的发言：界面用“提示”一行中文交代，不显示英文正文。"""

from __future__ import annotations

from uuid import UUID, uuid4

from harnessix.agent.completion_guard import completion_notice_item_id
from harnessix.agent.truncation_recovery import recovery_notice_item_id
from harnessix.product_ui.projection import ProductViewState, ProjectedItem
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import PublicItem, PublicTextContent
from tests.product_ui.test_tui_usability import _thread

HOST_NOTICE = "[Harnessix host notice] "


def _entry(turn_id: UUID, item_id: UUID, kind: str, text: str) -> ProjectedItem:
    return ProjectedItem(
        item=PublicItem(
            item_id=item_id, status="completed", content=PublicTextContent(kind=kind, text=text)
        ),
        turn_id=turn_id,
        first_cursor=1,
        last_cursor=1,
        final=True,
    )


def test_host_notices_are_shown_as_hints_not_as_the_user():
    thread = _thread()
    turn_id, other_turn = uuid4(), uuid4()
    steering = uuid4()
    view = ProductViewState(
        thread=thread,
        durable_cursor=2,
        items=(
            _entry(turn_id, uuid4(), "user_message", "修复这个缺陷"),
            _entry(turn_id, uuid4(), "assistant_message", "我先看一下。"),
            _entry(
                turn_id,
                recovery_notice_item_id(turn_id, 2),
                "user_message",
                HOST_NOTICE + "Your last response was empty",
            ),
            _entry(turn_id, steering, "user_message", "顺便更新文档"),
            _entry(
                turn_id,
                completion_notice_item_id(turn_id, 5),
                "user_message",
                HOST_NOTICE + "Your last response had no tool call",
            ),
            # 另一个Turn里只有用户自己的输入：照常显示。
            _entry(other_turn, uuid4(), "user_message", "再跑一次测试"),
        ),
    )

    lines = transcript_lines(view)

    assert [(line.role, line.text) for line in lines] == [
        ("你", "修复这个缺陷"),
        ("Harnessix", "我先看一下。"),
        ("提示", "模型的回复被截断或没有内容，已提示它继续"),
        ("你", "顺便更新文档"),
        ("提示", "模型停下时任务还没有做完，已提示它继续"),
        ("你", "再跑一次测试"),
    ]
    assert not any("Harnessix host notice" in line.text for line in lines)


def test_a_user_message_that_only_looks_like_a_notice_stays_the_users():
    """判定按Item身份，不按正文前缀：用户自己输入同样的文字仍显示为用户发言。"""
    thread = _thread()
    turn_id = uuid4()
    typed = HOST_NOTICE + "Your last response had no tool call"
    view = ProductViewState(
        thread=thread,
        durable_cursor=2,
        items=(
            _entry(turn_id, uuid4(), "user_message", "开始"),
            _entry(turn_id, uuid4(), "user_message", typed),
        ),
    )

    assert [(line.role, line.text) for line in transcript_lines(view)] == [
        ("你", "开始"),
        ("你", typed),
    ]
