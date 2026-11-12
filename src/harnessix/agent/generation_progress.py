"""模型请求进行中的生成进度：只作临时增量发布，不建Item、不进模型历史、不计入输出上限。

模型思考很久，或整段生成写文件的参数时，界面此前几分钟没有任何变化（思考预览到64 KiB为止，
工具参数完全没有增量），Token与费用也要等请求结束才更新，用户无从判断是不是卡住了。
这里按类别累计本步骤已生成的UTF-8字节数——思考、正文、工具参数——每秒至多发布一次，只有数字和
工具名，没有内容。每个模型步骤用确定的条目身份和独立序号，客户端据此与正文、思考预览区分。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Final, NamedTuple
from uuid import UUID, uuid5

from harnessix.agent.models import ItemDelta
from harnessix.models.contracts import ModelRequest, ProviderEvent, ToolArgumentsProgress

GENERATION_PROGRESS_VERSION: Final = "harnessix.generation-progress/v1"
PROGRESS_INTERVAL_SECONDS: Final = 1.0
_UNKNOWN_TOOL: Final = "-"


def generation_progress_item_id(turn_id: UUID, model_step: int) -> UUID:
    """某轮某步生成进度流的稳定身份；客户端据此区分进度与正文、思考预览。"""

    return uuid5(turn_id, f"{GENERATION_PROGRESS_VERSION}:{model_step}")


class GenerationCounts(NamedTuple):
    """一个模型步骤到目前为止生成的UTF-8字节数，按类别累计；tool是最近在生成参数的工具。"""

    reasoning: int = 0
    text: int = 0
    arguments: int = 0
    tool: str | None = None

    def line(self) -> str:
        """进度流的一行；每行都是累计值，客户端只需要最近一行。"""

        return f"{self.reasoning} {self.text} {self.arguments} {self.tool or _UNKNOWN_TOOL}\n"


def latest_counts(text: str) -> GenerationCounts | None:
    """读出进度流最近一行；流为空或不合格式时返回None。"""

    lines = text.splitlines()
    fields = lines[-1].split(" ") if lines else []
    if len(fields) != 4 or not all(field.isdecimal() for field in fields[:3]):
        return None
    reasoning, body, arguments = (int(field) for field in fields[:3])
    return GenerationCounts(
        reasoning, body, arguments, None if fields[3] == _UNKNOWN_TOOL else fields[3]
    )


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


@dataclass
class GenerationProgress:
    """一个模型步骤的生成进度：累计各类输出的字节数，有变化时每秒至多发布一次。"""

    request: ModelRequest
    emit: Callable[[ItemDelta], None]
    clock: Callable[[], float] = monotonic
    interval: float = PROGRESS_INTERVAL_SECONDS
    counts: GenerationCounts = GenerationCounts()
    sequence: int = 0
    _published: GenerationCounts | None = None
    _published_at: float | None = None
    _deferred: asyncio.TimerHandle | None = None

    def reasoning(self, delta: str) -> None:
        self._advance(self.counts._replace(reasoning=self.counts.reasoning + utf8_size(delta)))

    def text(self, delta: str) -> None:
        self._advance(self.counts._replace(text=self.counts.text + utf8_size(delta)))

    def observe(self, event: ProviderEvent) -> bool:
        """工具参数的进度事件在此消费并返回True；其他事件返回False交回原流程。"""

        if not isinstance(event, ToolArgumentsProgress):
            return False
        self._advance(self.counts._replace(arguments=event.utf8_bytes, tool=event.tool))
        return True

    def close(self) -> None:
        """步骤的响应已经结束：不再补发。"""

        if self._deferred is not None:
            self._deferred.cancel()
            self._deferred = None

    def _advance(self, counts: GenerationCounts) -> None:
        self.counts = counts
        wait = (
            0.0
            if self._published_at is None
            else (self._published_at + self.interval - self.clock())
        )
        if wait <= 0:
            self._publish()
        elif self._deferred is None:
            # 节流期内的变化到期补发：之后可能很久没有新事件。实测百炼在补丁内容（嵌套在数组里的
            # 参数）生成完之前不返回任何片段，开头的十个字节如果不补发，界面要等到整段结束才知道
            # 模型在生成修改。
            self._deferred = asyncio.get_running_loop().call_later(wait, self._publish)

    def _publish(self) -> None:
        self.close()
        counts = self.counts
        if counts == self._published:
            return
        self._published, self._published_at = counts, self.clock()
        self.sequence += 1
        self.emit(
            ItemDelta(
                thread_id=self.request.thread_id,
                turn_id=self.request.turn_id,
                item_id=generation_progress_item_id(self.request.turn_id, self.request.step),
                model_step=self.request.step,
                stream_sequence=self.sequence,
                delta=counts.line(),
            )
        )
