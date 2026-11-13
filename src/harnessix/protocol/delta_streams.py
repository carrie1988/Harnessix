"""公共增量属于哪一条流：思考预览复用正文增量通道，靠由轮次与步骤派生的条目身份区分。"""

from __future__ import annotations

from harnessix.agent.reasoning_preview import reasoning_item_id
from harnessix.protocol.contracts import PublicItemDelta


def is_reasoning_delta(delta: PublicItemDelta) -> bool:
    """该增量是模型的思考预览而不是助手正文；思考预览没有对应的Item。"""
    return delta.item_id == reasoning_item_id(delta.turn_id, delta.model_step)
