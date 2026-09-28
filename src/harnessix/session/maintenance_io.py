"""维护文件操作的单任务所有权、合作期限和取消收敛；不遗留后台写入者。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Event
from time import monotonic

from harnessix.agent.errors import KernelError


def require_legacy_maintenance(publication: object | None) -> None:
    """旧Plan/Progress和数据库备份尚无独立认证，不得用于认证Store的维护授权。"""
    if publication is not None:
        raise KernelError("maintenance_authenticated_unavailable", "认证Store的清理与恢复暂不可用")


class MaintenanceIOControl:
    """工作线程共享停止信号；SQLite进度回调和文件分块共同消费同一期限。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self._deadline = monotonic() + timeout
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def checkpoint(self) -> None:
        if self._cancelled.is_set():
            raise KernelError("maintenance_io_cancelled", "Store维护文件操作已取消")
        if monotonic() >= self._deadline:
            raise KernelError("maintenance_io_timeout", "Store维护文件操作超时")

    def interrupt(self) -> int:
        """SQLite回调不得抛Python异常；上层捕获中断后重新检查固定原因。"""
        try:
            self.checkpoint()
        except KernelError:
            return 1
        return 0


async def run_maintenance_io[T](
    operation: Callable[[MaintenanceIOControl], T], *, budget_seconds: float = 30.0
) -> T:
    """只派发一次；父取消后发停止信号并结算原任务，再传播取消。"""
    control = MaintenanceIOControl(budget_seconds)
    task = asyncio.create_task(asyncio.to_thread(operation, control))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        control.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # 取走工作线程异常；父取消始终优先，不把工作线程失败替换为业务成功。
        if not task.cancelled():
            task.exception()
        raise
