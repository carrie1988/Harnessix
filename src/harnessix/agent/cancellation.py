from __future__ import annotations

import asyncio
from collections.abc import Awaitable
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

    async def run(self, operation: Awaitable[T]) -> T:
        """托管可协作取消的 I/O；无论成功、取消还是父 Task 退出都回收子任务。"""
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
            await asyncio.gather(task, waiter, return_exceptions=True)
