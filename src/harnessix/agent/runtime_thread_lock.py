"""记录 Runtime Thread 锁的实际持锁 Task，不改变标准库的等待与取消语义。"""

from __future__ import annotations

import asyncio
from typing import Literal

from harnessix.agent.errors import KernelError


class RuntimeThreadLock(asyncio.Lock):
    """只允许成功获取锁的原 Task 释放；锁保持非重入。"""

    def __init__(self) -> None:
        super().__init__()
        self._owner: asyncio.Task[object] | None = None

    async def acquire(self) -> Literal[True]:
        task = asyncio.current_task()
        if task is None:
            raise KernelError("runtime_thread_lock_unowned", "Thread 锁必须由 asyncio Task 获取")
        acquired = await super().acquire()
        # 成功获取后到登记之间没有挂起点；等待取消不会改写原持锁者。
        self._owner = task
        return acquired

    def require_current_owner(self) -> None:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None or not self.locked() or self._owner is not task:
            raise KernelError("runtime_thread_lock_unowned", "当前 Task 未持有 Runtime Thread 锁")

    def release(self) -> None:
        self.require_current_owner()
        super().release()
        self._owner = None
