"""模型请求进行中的生成进度：只作临时增量发布，不建Item、不进模型历史、不计入输出上限。

模型思考很久，或整段生成写文件的参数时，界面此前几分钟没有任何变化（思考预览到64 KiB为止，
工具参数完全没有增量），Token与费用也要等请求结束才更新，用户无从判断是不是卡住了。
这里按类别累计本步骤已生成的UTF-8字节数——思考、正文、工具参数——每秒至多发布一次，只有数字和
工具名，没有内容。每个模型步骤用确定的条目身份和独立序号，客户端据此与正文、思考预览区分。
同一步骤的请求失败后自动重试时，这一行还带上第几次尝试与上次失败的类别（只有固定的几个词），
界面据此说明为什么还在等（Beta Issue #11）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Final, NamedTuple
from uuid import UUID, uuid5

from harnessix.agent.models import ItemDelta
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted
from harnessix.models.contracts import ModelRequest, ProviderEvent, ToolArgumentsProgress

GENERATION_PROGRESS_VERSION: Final = "harnessix.generation-progress/v1"
PROGRESS_INTERVAL_SECONDS: Final = 1.0
_UNKNOWN_TOOL: Final = "-"
# 失败尝试的错误码到公开的类别；只发布类别，不发布原始错误。
_FAILURE_KINDS: Final = {
    "provider_transport": "network",
    "provider_rate_limit": "rate_limit",
    "provider_provider_internal": "server",
}
FAILURE_KINDS: Final = frozenset({*_FAILURE_KINDS.values(), "other"})


def generation_progress_item_id(turn_id: UUID, model_step: int) -> UUID:
    """某轮某步生成进度流的稳定身份；客户端据此区分进度与正文、思考预览。"""

    return uuid5(turn_id, f"{GENERATION_PROGRESS_VERSION}:{model_step}")


class GenerationCounts(NamedTuple):
    """一个模型步骤到目前为止生成的UTF-8字节数，按类别累计；tool是最近在生成参数的工具。"""

    reasoning: int = 0
    text: int = 0
    arguments: int = 0
    tool: str | None = None
    # 本步骤第几次尝试，与上一次失败尝试的类别（FAILURE_KINDS之一）。
    attempt: int = 1
    failure: str | None = None

    def line(self) -> str:
        """进度流的一行；每行都是累计值，客户端只需要最近一行。没有重试时只有前四项。"""

        line = f"{self.reasoning} {self.text} {self.arguments} {self.tool or _UNKNOWN_TOOL}"
        if self.attempt > 1 or self.failure is not None:
            line += f" {self.attempt} {self.failure or _UNKNOWN_TOOL}"
        return line + "\n"


def latest_counts(text: str) -> GenerationCounts | None:
    """读出进度流最近一行；流为空或不合格式时返回None。"""

    lines = text.splitlines()
    fields = lines[-1].split(" ") if lines else []
    if len(fields) not in {4, 6} or not all(field.isdecimal() for field in fields[:3]):
        return None
    reasoning, body, arguments = (int(field) for field in fields[:3])
    counts = GenerationCounts(
        reasoning, body, arguments, None if fields[3] == _UNKNOWN_TOOL else fields[3]
    )
    if len(fields) == 4:
        return counts
    attempt, failure = fields[4], fields[5]
    if not attempt.isdecimal() or failure not in {*FAILURE_KINDS, _UNKNOWN_TOOL}:
        return None
    return counts._replace(
        attempt=int(attempt), failure=None if failure == _UNKNOWN_TOOL else failure
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

    def attempt(self, event: ModelAttemptStarted | ModelAttemptFinished) -> None:
        """记下失败尝试的类别；重试开始时发布第几次尝试，让界面说明为什么还在等。"""

        if isinstance(event, ModelAttemptFinished):
            if event.error is not None:
                kind = _FAILURE_KINDS.get(event.error.code, "other")
                self.counts = self.counts._replace(failure=kind)
        elif event.index > 1:
            # 新尝试从头生成：上一次断开前的思考与参数不再计入估算。重试要马上让用户知道，不节流。
            self.counts = GenerationCounts(attempt=event.index, failure=self.counts.failure)
            self._publish()

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
