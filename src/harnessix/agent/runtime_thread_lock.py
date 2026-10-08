"""记录 Runtime Thread 锁的实际持锁 Task，不改变标准库的等待与取消语义。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from harnessix.agent.errors import KernelError


class RuntimeThreadLock(asyncio.Lock):
    """只允许成功获取锁的原 Task 释放；锁保持非重入。"""

    def __init__(self) -> None:
        super().__init__()
        self._owner: asyncio.Task[object] | None = None
        self._owner_generation: object | None = None

    async def acquire(self) -> Literal[True]:
        task = asyncio.current_task()
        if task is None:
            raise KernelError("runtime_thread_lock_unowned", "Thread 锁必须由 asyncio Task 获取")
        acquired = await super().acquire()
        # 成功获取后到登记之间没有挂起点；等待取消不会改写原持锁者。
        self._owner = task
        self._owner_generation = object()
        return acquired

    def require_current_owner(self) -> None:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None or not self.locked() or self._owner is not task:
            raise KernelError("runtime_thread_lock_unowned", "当前 Task 未持有 Runtime Thread 锁")

    def observe_owner(self) -> Callable[[], None]:
        """由当前持锁 Task 签发本次持锁的只读观察函数，不授予持锁或释放权限。"""
        RuntimeThreadLock.require_current_owner(self)
        owner = self._owner
        generation = self._owner_generation

        def observer() -> None:
            # 冻结原 Task 与本次代际；同一 Task 再次持锁也不能使旧观察函数恢复有效。
            if (
                not self.locked()
                or self._owner is not owner
                or self._owner_generation is not generation
            ):
                raise KernelError(
                    "runtime_thread_lock_unowned", "当前 Task 未持有 Runtime Thread 锁"
                )

        return observer

    def release(self) -> None:
        self.require_current_owner()
        super().release()
        self._owner = None
        self._owner_generation = None
