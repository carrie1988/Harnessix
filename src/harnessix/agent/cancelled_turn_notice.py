"""上一轮被用户取消时，新一轮在用户输入之后补一条宿主说明。

模型历史收录每一轮已完成的Item，却不含轮次终态；被取消的请求在模型看来仍是“做到一半”，
用户接着问别的事时，模型会把两者混在一起或反问是否继续（2026-11-10 macOS实测）。
说明落为已完成的用户消息Item，身份由新Turn派生，重放确定；不改写任何既有Item或历史视图。
"""

from __future__ import annotations

from uuid import UUID, uuid5

from harnessix.agent.models import (
    EventPayload,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    Thread,
    TurnStatus,
)

CANCELLED_TURN_NOTICE_VERSION = "harnessix.cancelled-turn-notice/v1"
CANCELLED_TURN_NOTICE = (
    "[Harnessix host notice] The user cancelled the previous task before it finished. "
    "Do not resume it unless the latest message asks for that; respond to the latest message."
)


def cancelled_turn_notice_item_id(turn_id: UUID) -> UUID:
    """新Turn内这条说明的稳定身份；界面据此识别并不再重复显示。"""

    return uuid5(turn_id, CANCELLED_TURN_NOTICE_VERSION)


def cancelled_turn_notice_payloads(
    thread: Thread, turn_id: UUID, *, retry_of_turn_id: UUID | None
) -> list[EventPayload]:
    """Thread最近一轮由用户取消、且新Turn不是重试它时返回说明事件；否则为空。"""

    if retry_of_turn_id is not None or not thread.turns:
        return []
    if thread.turns[-1].status != TurnStatus.CANCELLED:
        return []
    content = TextContent(kind="user_message", text=CANCELLED_TURN_NOTICE)
    item_id = cancelled_turn_notice_item_id(turn_id)
    return [
        ItemStarted(item_id=item_id, content=content),
        ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
    ]
