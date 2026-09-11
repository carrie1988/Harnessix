"""公共Agent Protocol：在明确版本窗口内读取旧协议通知。"""

from __future__ import annotations

from collections.abc import Mapping

from harnessix.protocol.contracts import JsonRpcNotification

SERVER_NOTIFICATION_METHODS = frozenset(
    {
        "item/cancelled",
        "item/completed",
        "item/failed",
        "item/started",
        "thread/updated",
        "turn/completed",
        "turn/started",
        "turn/stateChanged",
        "usage/updated",
    }
)


def decode_known_notification(value: Mapping[str, object]) -> JsonRpcNotification | None:
    """验证Envelope；未知通知由旧客户端忽略，而不是误作已知事件。"""

    notification = JsonRpcNotification.model_validate(value)
    if notification.method not in SERVER_NOTIFICATION_METHODS:
        return None
    return notification
