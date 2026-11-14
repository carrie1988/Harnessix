"""模型Context来源：同一Turn内只观察一次的来源包装（ADR 0153）。

系统指令排在每个模型请求的最前面。其中任何一段正文变化，服务端的前缀缓存从该处起全部失效，
整段对话历史要重新计算；自托管服务上这是一次与历史长度成正比的等待，按量计费的服务上是一次全价输入。
一级目录概览正是这样一段会在Turn中途变化的正文：模型在工作区根目录新建或删除一个条目，概览就变。
模型已经从自己的工具结果里知道这些变化，概览只用于开局定位，所以同一Turn内沿用首次观察，下一Turn再刷新。

非职责：不改变被包装来源的观察方式与正文；不跨Turn保留观察；不落盘，进程重启后首个步骤重新观察。
"""

from __future__ import annotations

from collections import OrderedDict
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import (
    ContextBuildInput,
    ContextFragmentKind,
    ContextSourceObservation,
)
from harnessix.context.sources import ContextSource

# 一个产品进程同时活跃的Thread很少；超出时淘汰最久未用的，被淘汰的Thread下一步重新观察。
_MAX_THREADS = 32


class TurnStableContextSource:
    """把一个来源的观察固定在当前Turn的首个模型步骤上。"""

    def __init__(self, source: ContextSource) -> None:
        self._source = source
        self._observed: OrderedDict[UUID, tuple[UUID, str, ContextSourceObservation]] = (
            OrderedDict()
        )

    @property
    def source_id(self) -> str:
        return self._source.source_id

    @property
    def kind(self) -> ContextFragmentKind:
        return self._source.kind

    async def observe(
        self, request: ContextBuildInput, cancel: CancelToken
    ) -> ContextSourceObservation:
        held = self._observed.get(request.thread_id)
        if held is not None and held[:2] == (request.turn_id, request.workspace):
            self._observed.move_to_end(request.thread_id)
            return held[2]
        observation = await self._source.observe(request, cancel)
        self._observed[request.thread_id] = (request.turn_id, request.workspace, observation)
        self._observed.move_to_end(request.thread_id)
        while len(self._observed) > _MAX_THREADS:
            self._observed.popitem(last=False)
        return observation
