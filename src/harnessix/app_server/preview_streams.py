"""客户端声明的预览流：思考预览与生成进度都复用正文Delta通道，按条目身份区分。

没有声明某一种预览流的客户端不会收到它——这样的客户端会把思考内容或进度行当成助手正文显示。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harnessix.agent.generation_progress import generation_progress_item_id
from harnessix.agent.reasoning_preview import reasoning_item_id
from harnessix.protocol.contracts import EventsNextResult


@dataclass(frozen=True, slots=True)
class PreviewStreams:
    """握手时确定的预览流开关；两种都只在客户端同时开启item_deltas时生效。"""

    reasoning: bool = False
    progress: bool = False

    def visible(self, page: EventsNextResult) -> EventsNextResult:
        """去掉客户端没有声明的预览流增量；正文增量与其余内容原样返回。"""

        hidden = [
            identity
            for wanted, identity in (
                (self.reasoning, reasoning_item_id),
                (self.progress, generation_progress_item_id),
            )
            if not wanted
        ]
        kept = tuple(
            delta
            for delta in page.deltas
            if all(
                delta.item_id != identity(delta.turn_id, delta.model_step) for identity in hidden
            )
        )
        return page if kept == page.deltas else page.model_copy(update={"deltas": kept})


NO_PREVIEWS: Final = PreviewStreams()
