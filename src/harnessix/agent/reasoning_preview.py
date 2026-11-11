"""模型思考内容的实时预览：只作临时增量发布，不建Item、不进模型历史、不计入输出上限。

思考或整段生成工具参数时，流中可能几十秒没有正文（百炼qwen3.8实测13～27秒）。预览让界面实时看到
模型在想什么。每个模型步骤用确定的条目身份和独立序号，避免占用正文增量序号而让界面误判正文缺口；
发布前同样经过公开文本保护，单步最多发布64 KiB，超出后只停止预览，不影响模型步骤本身。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid5

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import ItemDelta
from harnessix.agent.publication import PublicTextStep, protect_text
from harnessix.models.contracts import ModelRequest, ProviderEvent, ReasoningDelta

REASONING_PREVIEW_VERSION = "harnessix.reasoning-preview/v1"
REASONING_CONTENT_ID = "harnessix.reasoning"
MAX_REASONING_PREVIEW_BYTES = 64 * 1024


def reasoning_item_id(turn_id: UUID, model_step: int) -> UUID:
    """某轮某步思考预览流的稳定身份；客户端据此区分思考与正文。"""

    return uuid5(turn_id, f"{REASONING_PREVIEW_VERSION}:{model_step}")


@dataclass
class ReasoningPreview:
    """一个模型步骤的思考预览：独立序号、单步上限，思考结束时从公开保护中放出被扣住的尾部。"""

    request: ModelRequest
    token: CancelToken
    protection: PublicTextStep | None
    emit: Callable[[ItemDelta], None]
    published_bytes: int = 0
    sequence: int = 0
    ended: bool = False

    async def observe(self, event: ProviderEvent) -> bool:
        """思考增量在此消费并返回True；其他事件到来即视为思考结束，返回False交回原流程。"""

        if not isinstance(event, ReasoningDelta):
            await self._end()
            return False
        size = len(event.delta.encode("utf-8", "replace"))
        if not self.ended and self.published_bytes + size <= MAX_REASONING_PREVIEW_BYTES:
            self.published_bytes += size
            token, delta = self.token, event.delta
            self._emit(await protect_text(self.protection, REASONING_CONTENT_ID, delta, token))
        return True

    async def _end(self) -> None:
        # 公开保护会扣住末尾可能构成密钥的字节，收尾才放出；之后本步骤的思考不再预览。
        if self.ended or not self.published_bytes:
            return
        self.ended = True
        self._emit(await protect_text(self.protection, REASONING_CONTENT_ID, None, self.token))

    def _emit(self, prefix: str) -> None:
        if not prefix:
            return
        self.sequence += 1
        self.emit(
            ItemDelta(
                thread_id=self.request.thread_id,
                turn_id=self.request.turn_id,
                item_id=reasoning_item_id(self.request.turn_id, self.request.step),
                model_step=self.request.step,
                stream_sequence=self.sequence,
                delta=prefix,
            )
        )
