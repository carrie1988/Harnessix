"""过期Artifact正文的自动回收：Runtime打开时回收一次，运行期间定期回收（ADR 0164）。"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Protocol, runtime_checkable
from uuid import UUID

from harnessix.artifacts.contracts import CollectionReport

_LOGGER = logging.getLogger(__name__)

# 每批在一个写事务里完成；批次小，宿主同时提交的事件等待时间短。
RECLAIM_BATCH = 50
RECLAIM_INTERVAL_SECONDS = 3600.0
# 打开Runtime时最多等这么久；积压更多时剩余部分立即转入后台。
STARTUP_BUDGET_SECONDS = 2.0


@runtime_checkable
class ArtifactCollector(Protocol):
    async def collect(self, *, limit: int = 100, after: UUID | None = None) -> CollectionReport: ...


class ArtifactReclaimer:
    """按游标扫完一轮到期正文；任何失败只记日志，等下一轮，不影响宿主与Turn。"""

    def __init__(
        self,
        store: ArtifactCollector,
        *,
        interval_seconds: float = RECLAIM_INTERVAL_SECONDS,
        startup_budget_seconds: float = STARTUP_BUDGET_SECONDS,
    ) -> None:
        self._store = store
        self._interval = interval_seconds
        self._startup_budget = startup_budget_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        finished = await self._sweep(deadline=monotonic() + self._startup_budget)
        self._task = asyncio.create_task(self._periodic(0.0 if not finished else self._interval))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _periodic(self, delay: float) -> None:
        while True:
            await asyncio.sleep(delay)
            await self._sweep(deadline=None)
            delay = self._interval

    async def _sweep(self, *, deadline: float | None) -> bool:
        """返回是否扫完一轮；到达deadline时提前返回False。"""
        after: UUID | None = None
        try:
            while True:
                report = await self._store.collect(limit=RECLAIM_BATCH, after=after)
                after = report.next_after
                if after is None:
                    return True
                if deadline is not None and monotonic() >= deadline:
                    return False
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning("过期Artifact回收失败，等待下一轮：%s", type(error).__name__)
            return True


async def start_artifact_reclaim(store: object) -> ArtifactReclaimer | None:
    """只有能回收的Artifact存储才启动；其他发布器（测试替身等）不调度。"""
    if not isinstance(store, ArtifactCollector):
        return None
    reclaimer = ArtifactReclaimer(store)
    await reclaimer.start()
    return reclaimer
