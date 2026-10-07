"""持久Agent状态机：提供可协作传播的Turn取消令牌。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class TurnCancelled(Exception):
    """领域取消信号，与调用方取消整个 asyncio Task 区分。"""


class CancelToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise TurnCancelled

    async def run(self, operation: Awaitable[T], *, preserve_failure: bool = False) -> T:
        """回收子任务；显式托管效果结算时，结算失败不得被外层取消覆盖。"""
        task = asyncio.ensure_future(operation)
        waiter = asyncio.create_task(self._event.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                raise TurnCancelled
            return task.result()
        finally:
            for child in (task, waiter):
                if not child.done():
                    child.cancel()
            try:
                await asyncio.gather(task, waiter, return_exceptions=True)
            finally:
                if preserve_failure and task.done() and not task.cancelled():
                    failure = task.exception()
                    if failure is not None and not isinstance(failure, TurnCancelled):
                        raise failure


def parent_cancel_checkpointer(check: Callable[[], None]) -> Callable[[], None]:
    """只传播本次操作新增的父Task取消；入口仍须异步交付既有待取消。"""
    task = asyncio.current_task()
    initial_count = task.cancelling() if task is not None else 0

    def checkpoint() -> None:
        check()
        if task is not None and task.cancelling() > initial_count:
            raise asyncio.CancelledError

    return checkpoint
